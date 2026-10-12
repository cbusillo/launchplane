from pathlib import Path
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
from typing import cast
from unittest import TestCase
from unittest.mock import Mock, patch

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    build_merge_train_batch_candidate_record,
)
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.merge_train_admission import build_merge_train_controller_status_read_model
from control_plane.merge_train_candidate_failures import CandidateCheckFailures
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerLeaseContext,
    MergeTrainControllerRunOnceEnvelope,
    execute_merge_train_controller_with_client,
)
from control_plane.merge_train_github import GitHubMergeTrainClient, MergeTrainGitHubError
from control_plane.merge_train_diagnostics import controller_last_result
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.test_merge_train_culprit_isolation import _TrainSimulation, TEST_FAILURE
from tests.test_merge_train_policy_recovery import _NoAdmissionEvaluator
from tests.test_merge_admission_records import _guard_records


class RecoveryDiagnosticsTests(TestCase):
    def test_unchanged_landing_evidence_in_a_narrower_projection_is_not_progress(self) -> None:
        candidate, landing, state, _ = _guard_records()
        planned, key = controller_last_result(
            result={
                "controller_action": "plan_landing",
                "candidate": candidate.candidate.model_dump(mode="json"),
                "landing_plan": landing.landing_plan.model_dump(mode="json"),
            },
            state=state,
            observed_at=state.updated_at,
            observations={},
            pass_started_at=state.updated_at,
            trace_id="projection",
            policy_key=state.policy_key,
            policy_sha256=state.policy_sha256,
            reconciliation_required=False,
            persisted=True,
        )
        later = (
            (
                datetime.fromisoformat(state.updated_at.replace("Z", "+00:00"))
                + timedelta(seconds=30)
            )
            .isoformat()
            .replace("+00:00", "Z")
        )
        blocked, following = controller_last_result(
            result={
                "controller_action": "block",
                "landing_plan": landing.landing_plan.model_dump(mode="json"),
            },
            state=state.model_copy(update={"last_result": planned, "semantic_progress_key": key}),
            observed_at=later,
            observations={},
            pass_started_at=state.updated_at,
            trace_id="projection",
            policy_key=state.policy_key,
            policy_sha256=state.policy_sha256,
            reconciliation_required=False,
            persisted=True,
        )
        self.assertEqual(blocked.semantic_progress_at, planned.semantic_progress_at)
        self.assertEqual(following, key)

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.store = FilesystemRecordStore(self.path)
        self.train = _TrainSimulation(self, self.store, 4)
        self.policy = build_test_merge_train_policy_record(
            repository=self.train.snapshot.repository
        )
        candidate = self.train._plan_from_queue().candidate.model_copy(
            update={
                "policy_sha256": self.policy.policy_sha256,
                "candidate_sha": "failed-sha",
                "status": "failed",
                "required_checks_status": "fail",
            }
        )
        self.failed = build_merge_train_batch_candidate_record(
            candidate=candidate, source="test", updated_at=candidate.updated_at
        )
        self.store.write_merge_train_batch_candidate_record(self.failed)
        self.client = Mock()
        self.client.read_merge_train_snapshot.return_value = self.train.snapshot
        self.client.observe_batch_candidate_checks.return_value = candidate
        self.client.read_candidate_check_failures.return_value = TEST_FAILURE
        self.client.read_base_check_status.return_value = "pending"
        self.attempt = 0

    def run_pass(self, *, mutate: bool = True) -> dict[str, object]:
        self.attempt += 1
        with (
            patch(
                "control_plane.merge_train_controller_run_once.changed_closed_batch_body",
                return_value=False,
            ),
            patch(
                "control_plane.merge_train_controller_run_once.bound_closed_batch_pull_request",
                return_value={"number": 9000},
            ),
        ):
            result = execute_merge_train_controller_with_client(
                request=MergeTrainControllerRunOnceEnvelope(
                    repository=self.train.snapshot.repository, mutate=mutate
                ),
                policy=self.policy.policy,
                policy_sha256=self.policy.policy_sha256,
                repository_policy=self.policy.policy.policies[0],
                github_client=cast(GitHubMergeTrainClient, self.client),
                trace_id=f"diagnostic-{self.attempt}",
                recorded_at=self.failed.updated_at,
                candidate_store=self.store,
                landing_store=self.store,
                stack_collapse_store=self.store,
                controller_state_store=self.store,
                admission_store=self.store,
                admission_evaluator=_NoAdmissionEvaluator(),
            )
        return result.accepted_result

    def state(self) -> MergeTrainControllerStateRecord:
        return self.store.list_merge_train_controller_state_records()[0]

    def test_base_wait_survives_release_restart_and_repeated_heartbeats(self) -> None:
        result = self.run_pass()
        state = self.state()
        diagnosis = state.last_result
        assert diagnosis is not None
        self.assertEqual(state.status, "idle")
        self.assertFalse(state.lease_owner)
        self.assertEqual(diagnosis.reason_code, "base_checks_not_green")
        self.assertEqual(diagnosis.base_checks_status, "pending")
        self.assertEqual(diagnosis.source_failed_record_id, self.failed.record_id)
        self.assertEqual(result["last_controller_result"], diagnosis.model_dump(mode="json"))
        self.store = FilesystemRecordStore(self.path)
        self.run_pass()
        renewed = self.state().last_result
        assert renewed is not None
        self.assertEqual(renewed.semantic_progress_at, diagnosis.semantic_progress_at)
        self.assertNotEqual(renewed.trace_id, diagnosis.trace_id)
        read_model = build_merge_train_controller_status_read_model(
            store=self.store,
            repository=state.repository,
            base_branch=state.base_branch,
            generated_at=self.state().updated_at,
            current_policy_key=self.policy.policy.policies[0].policy_key,
            current_policy_sha256=self.policy.policy_sha256,
        )
        self.assertEqual(read_model.last_controller_result, self.state().last_result)
        self.assertEqual(read_model.last_controller_result_policy_status, "current")

    def test_pending_original_heads_stop_before_candidate_or_base_reads(self) -> None:
        snapshot = self.train.snapshot
        self.client.read_merge_train_snapshot.return_value = snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    pr.model_copy(update={"required_checks_status": "pending"})
                    for pr in snapshot.pull_requests
                )
            }
        )
        result = self.run_pass()
        diagnosis = self.state().last_result
        assert diagnosis is not None
        self.assertEqual(result["controller_action"], "wait_for_checks")
        self.assertEqual(diagnosis.reason_code, "original_head_checks_pending")
        self.assertEqual(diagnosis.candidate_checks_status, "not_observed")
        self.assertEqual(diagnosis.base_checks_status, "not_observed")
        self.assertEqual(diagnosis.source_failed_record_id, self.failed.record_id)
        self.client.observe_batch_candidate_checks.assert_not_called()

    def test_recovered_retry_exhaustion_is_distinct_from_base_wait(self) -> None:
        candidate = self.failed.candidate.model_copy(update={"check_retry_of": "earlier-failure"})
        self.failed = build_merge_train_batch_candidate_record(
            candidate=candidate, source="retry", updated_at=candidate.updated_at
        )
        self.store = FilesystemRecordStore(self.path / "retry")
        self.store.write_merge_train_batch_candidate_record(self.failed)
        self.client.observe_batch_candidate_checks.return_value = candidate.model_copy(
            update={"required_checks_status": "pass", "status": "passed"}
        )
        self.run_pass()
        diagnosis = self.state().last_result
        assert diagnosis is not None
        self.assertEqual(diagnosis.reason_code, "candidate_check_retry_used")
        self.assertEqual(diagnosis.candidate_checks_status, "pass")
        self.assertEqual(diagnosis.base_checks_status, "not_observed")
        self.assertEqual(diagnosis.check_retry_of, candidate.check_retry_of)
        self.assertEqual(diagnosis.next_actor, "repository_agent")
        self.client.read_base_check_status.assert_not_called()

    def test_missing_failure_evidence_and_transient_read_keep_their_guard(self) -> None:
        for failures, reason in (
            (CandidateCheckFailures(checks=()), "candidate_failure_evidence_unavailable"),
            (
                MergeTrainGitHubError("private provider body", status_code=503),
                "candidate_failure_read_unavailable",
            ),
        ):
            with self.subTest(reason=reason):
                self.client.read_candidate_check_failures.side_effect = (
                    failures if isinstance(failures, Exception) else None
                )
                self.client.read_candidate_check_failures.return_value = failures
                self.run_pass()
                diagnosis = self.state().last_result
                assert diagnosis is not None
                self.assertEqual(diagnosis.reason_code, reason)
                self.assertNotIn("private provider body", diagnosis.model_dump_json())
                self.assertEqual(diagnosis.base_checks_status, "not_observed")

    def test_isolation_has_failed_source_and_green_base_without_changing_members(self) -> None:
        self.client.read_base_check_status.return_value = "pass"
        result = self.run_pass()
        diagnosis = self.state().last_result
        assert diagnosis is not None
        self.assertEqual(result["reason_code"], "failure_isolation")
        self.assertEqual(diagnosis.reason_code, result["reason_code"])
        self.assertEqual(diagnosis.source_failed_record_id, self.failed.record_id)
        self.assertEqual(diagnosis.base_checks_status, "pass")
        candidate = cast(dict[str, object], result["candidate"])
        entries = cast(list[dict[str, object]], candidate["entries"])
        self.assertEqual(
            [entry["pull_request_number"] for entry in entries],
            [entry.pull_request_number for entry in self.failed.candidate.entries[:2]],
        )

    def test_real_replacement_advances_the_semantic_progress_time(self) -> None:
        self.run_pass()
        waiting = self.state().last_result
        assert waiting is not None
        self.client.read_base_check_status.return_value = "pass"
        self.run_pass()
        replacement = self.state().last_result
        assert replacement is not None
        self.assertGreater(replacement.semantic_progress_at, waiting.semantic_progress_at)

    def test_isolation_check_wait_keeps_its_source_across_restart(self) -> None:
        self.client.read_base_check_status.return_value = "pass"
        result = self.run_pass()
        candidate = MergeTrainBatchCandidate.model_validate(result["candidate"]).model_copy(
            update={
                "status": "ready_for_checks",
                "candidate_sha": "isolation-sha",
                "required_checks_status": "pending",
            }
        )
        record = build_merge_train_batch_candidate_record(
            candidate=candidate, source="built", updated_at=candidate.updated_at
        )
        self.store.write_merge_train_batch_candidate_record(record)
        self.store = FilesystemRecordStore(self.path)
        self.client.observe_batch_candidate_checks.return_value = candidate
        self.run_pass()
        diagnosis = self.state().last_result
        assert diagnosis is not None
        self.assertEqual(diagnosis.reason_code, "failure_isolation_checks_pending")
        self.assertEqual(diagnosis.source_failed_record_id, self.failed.record_id)
        self.assertEqual(diagnosis.observed_candidate_sha, candidate.candidate_sha)
        self.assertEqual(diagnosis.candidate_checks_status, "pending")
        self.client.observe_batch_candidate_checks.side_effect = MergeTrainGitHubError(
            "temporary read", status_code=503
        )
        with self.assertRaises(MergeTrainGitHubError):
            self.run_pass()
        interrupted = self.state().last_result
        assert interrupted is not None
        self.assertEqual(interrupted.source_failed_record_id, self.failed.record_id)
        self.assertEqual(interrupted.recovery_kind, diagnosis.recovery_kind)
        self.store = FilesystemRecordStore(self.path)
        self.client.observe_batch_candidate_checks.side_effect = None
        self.run_pass()
        resumed = self.state().last_result
        assert resumed is not None
        self.assertEqual(resumed.reason_code, diagnosis.reason_code)
        self.assertEqual(resumed.source_failed_record_id, diagnosis.source_failed_record_id)
        observed_at = (
            (
                datetime.fromisoformat(self.state().updated_at.replace("Z", "+00:00"))
                + timedelta(seconds=1)
            )
            .isoformat()
            .replace("+00:00", "Z")
        )
        self.client.observe_batch_candidate_checks.return_value = candidate.model_copy(
            update={"required_checks_status": "pass", "status": "passed"}
        )
        with patch(
            "control_plane.merge_train_controller_run_once.utc_now_timestamp",
            return_value=observed_at,
        ):
            self.run_pass()
        changed = self.state().last_result
        assert changed is not None
        self.assertEqual(changed.semantic_progress_at, observed_at)
        self.assertNotEqual(changed.semantic_progress_at, candidate.updated_at)

    def test_historical_reobservation_binds_its_own_failed_source_and_sha(self) -> None:
        from control_plane.merge_train_controller_run_once import (
            _reobserve_failed_merge_train_candidate,
        )

        self.run_pass()
        lease = MergeTrainControllerLeaseContext(record=self.state())
        lease.observe_failed_candidate(self.failed)
        historical = build_merge_train_batch_candidate_record(
            candidate=self.failed.candidate.model_copy(
                update={
                    "candidate_sha": "historical-sha",
                    "entries": self.failed.candidate.entries[:1],
                }
            ),
            source="historical",
            updated_at=self.failed.updated_at,
        )
        self.client.observe_batch_candidate_checks.return_value = historical.candidate.model_copy(
            update={"required_checks_status": "pass", "status": "passed"}
        )
        result = _reobserve_failed_merge_train_candidate(
            candidate_store=self.store,
            active_candidate_record=historical,
            github_client=cast(GitHubMergeTrainClient, self.client),
            merge_method="merge",
            repository=historical.candidate.repository,
            base_branch=historical.candidate.base_branch,
            recorded_at=self.state().updated_at,
            trace_id="historical-observation",
            mutate=False,
            lease=lease,
            held_out=(),
        )
        assert result is not None
        lease.record_result(
            result,
            pass_started_at=self.state().updated_at,
            trace_id="historical-observation",
            reconciliation_required=False,
        )
        diagnosis = cast(dict[str, object], result["last_controller_result"])
        self.assertEqual(diagnosis["source_failed_record_id"], historical.record_id)
        self.assertEqual(diagnosis["observed_candidate_sha"], historical.candidate.candidate_sha)
        self.assertEqual(diagnosis["candidate_checks_status"], "pass")

    def test_dry_run_and_stale_policy_do_not_create_current_mutation_evidence(self) -> None:
        self.run_pass()
        state = self.state()
        self.run_pass(mutate=False)
        self.assertEqual(self.state(), state)
        read_model = build_merge_train_controller_status_read_model(
            store=self.store,
            repository=state.repository,
            base_branch=state.base_branch,
            generated_at=state.updated_at,
            current_policy_key=state.policy_key,
            current_policy_sha256="new-policy",
        )
        self.assertEqual(read_model.last_controller_result_policy_status, "stale")
        self.assertEqual(read_model.last_controller_result, state.last_result)

    def test_uncertain_provider_effect_remains_reconciliation_after_restart(self) -> None:
        def uncertain(*, lease: MergeTrainControllerLeaseContext, **_: object) -> dict[str, object]:
            lease.checkpoint(
                active_action="land_batch",
                active_phase="merge_pull_request",
                active_record_id="landing-record",
            )
            raise MergeTrainGitHubError("a private token must never be copied", status_code=503)

        with patch(
            "control_plane.merge_train_controller_run_once._advance_without_active_landing",
            side_effect=uncertain,
        ):
            with self.assertRaises(MergeTrainGitHubError):
                self.run_pass()
        self.store = FilesystemRecordStore(self.path)
        state = self.state()
        self.assertEqual(state.status, "reconcile_required")
        self.assertFalse(state.lease_owner)
        assert state.last_result is not None
        self.assertTrue(state.last_result.reconciliation_required)
        self.assertEqual(state.last_result.record_id, state.active_record_id)
        self.assertEqual(state.last_result.action, "land_batch")
        self.assertNotIn("private token", state.last_result.model_dump_json())

    def test_changed_policy_dry_run_binds_new_observation_and_preserves_stored_result(self) -> None:
        self.run_pass()
        prior = self.state()
        self.store = FilesystemRecordStore(self.path / "new-policy")
        self.store.write_merge_train_controller_state_record(prior)
        target = self.policy.policy.policies[0]
        changed = self.policy.policy.model_copy(
            update={
                "policies": (
                    target.model_copy(
                        update={"scheduler": target.scheduler.model_copy(update={"enabled": True})}
                    ),
                )
            }
        )
        self.policy = MergeTrainPolicyRecord(
            record_id="changed-policy", source="test", updated_at=prior.updated_at, policy=changed
        )
        self.client.read_merge_train_snapshot.return_value = self.train.snapshot.model_copy(
            update={"pull_requests": ()}
        )
        result = self.run_pass(mutate=False)
        diagnosis = cast(dict[str, object], result["last_controller_result"])
        self.assertNotEqual(self.policy.policy_sha256, prior.policy_sha256)
        self.assertEqual(diagnosis["policy_sha256"], self.policy.policy_sha256)
        self.assertEqual(diagnosis["policy_key"], target.policy_key)
        self.assertEqual(self.state(), prior)
