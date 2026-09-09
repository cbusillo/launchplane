from __future__ import annotations

import unittest
from typing import cast
from unittest.mock import Mock, patch

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_landing_plan,
    build_merge_train_batch_landing_plan_record,
    build_ordinary_merge_train_candidate_ref,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
    build_merge_train_controller_key,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentJobBinding
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerLeaseContext,
    MergeTrainControllerRequestError,
    MergeTrainControllerRunOnceEnvelope,
    _advance_active_landing_record,
    _finish_landed_merge_train_batch,
)
from control_plane.merge_admission import GuardedMergeAdmission
from control_plane.merge_train_github import (
    MergeTrainGitHubError,
    MergeTrainGitHubStaleHeadError,
)
from tests.merge_train_policy_fixtures import build_test_merge_train_policy


REPOSITORY = "example/merge-train-repo"
NOW = "2026-09-09T12:00:00Z"
POLICY_SHA256 = "1" * 64


class _ControllerStore:
    def __init__(self, record: MergeTrainControllerStateRecord) -> None:
        self.record = record

    def list_merge_train_controller_state_records(self, **kwargs: object) -> tuple[object, ...]:
        del kwargs
        return (self.record,)

    def compare_and_set_merge_train_controller_state_record(
        self,
        *,
        record: MergeTrainControllerStateRecord,
        expected_lease_owner: str,
        expected_lease_acquired_at: str,
        lease_seconds: int,
    ) -> MergeTrainControllerStateRecord:
        del lease_seconds
        if (
            self.record.lease_owner != expected_lease_owner
            or self.record.lease_acquired_at != expected_lease_acquired_at
        ):
            raise AssertionError("controller fence changed")
        self.record = record
        return record


class _CandidateStore:
    def __init__(self, record: MergeTrainBatchCandidateRecord) -> None:
        self.record = record

    def list_merge_train_batch_candidate_records(
        self, **kwargs: object
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]:
        del kwargs
        return (self.record,)


class _LandingStore:
    def __init__(self, record: MergeTrainBatchLandingPlanRecord) -> None:
        self.records = [record]

    def write_merge_train_batch_landing_plan_record(
        self, record: MergeTrainBatchLandingPlanRecord
    ) -> MergeTrainBatchLandingPlanRecord:
        self.records.append(record)
        return record

    def list_merge_train_batch_landing_plan_records(
        self, **kwargs: object
    ) -> tuple[MergeTrainBatchLandingPlanRecord, ...]:
        del kwargs
        return tuple(self.records)


class _NonReturningLandingStore:
    def __init__(self, record: MergeTrainBatchLandingPlanRecord) -> None:
        self.records = [record]

    def write_merge_train_batch_landing_plan_record(
        self, record: MergeTrainBatchLandingPlanRecord
    ) -> None:
        self.records.append(record)

    def list_merge_train_batch_landing_plan_records(
        self, **kwargs: object
    ) -> tuple[MergeTrainBatchLandingPlanRecord, ...]:
        del kwargs
        return tuple(self.records)


class _StackStore:
    def list_merge_train_stack_collapse_plan_records(self, **kwargs: object) -> tuple[object, ...]:
        del kwargs
        return ()


class _OrdinaryLandingClient:
    def __init__(self, *, stale: bool = False, cleanup: bool | Exception = False) -> None:
        self.transport = object()
        self.stale = stale
        self.cleanup = cleanup
        self.cleanup_calls = 0
        self.candidate_ref_exists_calls = 0

    def land_batch_candidate(self, **kwargs: object) -> MergeTrainBatchLandingPlan:
        if self.stale:
            raise MergeTrainGitHubStaleHeadError("head changed", status_code=409)
        plan = kwargs["landing_plan"]
        assert isinstance(plan, MergeTrainBatchLandingPlan)
        selected_index = next(
            index for index, entry in enumerate(plan.entries) if entry.status == "planned"
        )
        selected = plan.entries[selected_index]
        checkpoint = kwargs["checkpoint"]
        assert callable(checkpoint)
        merged = selected.model_copy(
            update={
                "status": "merged",
                "merge_commit_sha": f"merge-{selected.pull_request_number}",
            }
        )
        successor = plan.model_copy(
            update={
                "entries": (
                    *plan.entries[:selected_index],
                    merged,
                    *plan.entries[selected_index + 1 :],
                )
            }
        )
        persisted = checkpoint(successor, merged, "entry_merged")
        assert isinstance(persisted, MergeTrainBatchLandingPlanRecord)
        guard = cast(GuardedMergeAdmission, kwargs["admission_guard"])
        guard.update_landing_plan_record(persisted)
        return successor

    def candidate_ref_exists(self, **kwargs: object) -> bool:
        del kwargs
        self.candidate_ref_exists_calls += 1
        raise AssertionError("ordinary cleanup performed an ambient ref read")

    def cleanup_batch_candidate_ref(self, **kwargs: object) -> bool:
        del kwargs
        self.cleanup_calls += 1
        if isinstance(self.cleanup, Exception):
            raise self.cleanup
        return self.cleanup


class OrdinaryLandingControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binding = OrdinaryAgentJobBinding(
            request_id="ordinary_request",
            scope_sha256="a" * 64,
            binding_revision=1,
        )
        entries = (
            MergeTrainBatchEntry(pull_request_number=12, position=1, head_sha="b" * 40),
            MergeTrainBatchEntry(pull_request_number=13, position=2, head_sha="c" * 40),
        )
        candidate = MergeTrainBatchCandidate(
            batch_id="ordinary-batch",
            repository=REPOSITORY,
            base_branch="main",
            base_sha="a" * 40,
            policy_key=f"{REPOSITORY}:main",
            policy_sha256=POLICY_SHA256,
            candidate_ref=build_ordinary_merge_train_candidate_ref(
                binding=self.binding, batch_id="ordinary-batch"
            ),
            candidate_sha="d" * 40,
            status="passed",
            entries=entries,
            required_checks_status="pass",
            created_at=NOW,
            updated_at=NOW,
        )
        self.candidate_record = MergeTrainBatchCandidateRecord(
            ordinary_job_binding=self.binding,
            record_id="candidate-record",
            source="test",
            updated_at=NOW,
            candidate=candidate,
        )
        self.landing_record = build_merge_train_batch_landing_plan_record(
            ordinary_job_binding=self.binding,
            landing_plan=build_merge_train_batch_landing_plan(
                candidate=candidate,
                merge_method="merge",
                created_at=NOW,
            ),
            source="test",
            updated_at=NOW,
        )
        self.policy = build_test_merge_train_policy(repository=REPOSITORY).policies[0]
        self.request = MergeTrainControllerRunOnceEnvelope(
            repository=REPOSITORY,
            base_branch="main",
            mutate=True,
        )

    def test_three_polls_persist_one_entry_then_cleanup_without_ambient_read(self) -> None:
        landing_store = _LandingStore(self.landing_record)
        client = _OrdinaryLandingClient(cleanup=False)

        first_result, first_lease = self._advance(
            record=self.landing_record,
            landing_store=landing_store,
            client=client,
        )
        first_successor = landing_store.records[-1]
        self.assertEqual(first_result["landing_progress"], "partial")
        self.assertEqual(
            [entry.status for entry in first_successor.landing_plan.entries],
            ["merged", "planned"],
        )
        self.assertEqual(
            first_result["merge_train_batch_landing_plan_record_id"], first_successor.record_id
        )
        self.assertEqual(first_lease.record.step_payload["completed_entry_count"], 1)
        self.assertEqual(client.cleanup_calls, 0)

        second_result, second_lease = self._advance(
            record=first_successor,
            landing_store=landing_store,
            client=client,
        )
        terminal = landing_store.records[-1]
        self.assertEqual(second_result["landing_progress"], "cleanup_pending")
        self.assertEqual(
            [entry.status for entry in terminal.landing_plan.entries],
            ["merged", "merged"],
        )
        self.assertEqual(second_lease.record.step_payload["completed_entry_count"], 2)
        self.assertEqual(client.cleanup_calls, 0)

        third_result, third_lease = self._advance(
            record=terminal,
            landing_store=landing_store,
            client=client,
        )
        self.assertEqual(third_result["landing_progress"], "complete")
        self.assertEqual(third_result["candidate_ref_cleanup_status"], "retained")
        self.assertEqual(third_lease.record.step_payload["cleanup_status"], "retained")
        self.assertEqual(client.cleanup_calls, 1)
        self.assertEqual(client.candidate_ref_exists_calls, 0)
        self.assertEqual(len(landing_store.records), 3)

        resumed_result, _ = self._advance(
            record=terminal,
            landing_store=landing_store,
            client=client,
            step_payload=third_lease.record.step_payload,
        )
        self.assertEqual(resumed_result["landing_progress"], "complete")
        self.assertEqual(resumed_result["candidate_ref_cleanup_status"], "retained")
        self.assertEqual(client.cleanup_calls, 1)

    def test_terminal_dry_run_and_failed_cleanup_remain_cleanup_pending(self) -> None:
        terminal = self._terminal_record()
        dry_client = _OrdinaryLandingClient()
        dry_result, _ = self._advance(
            record=terminal,
            landing_store=_LandingStore(terminal),
            client=dry_client,
            mutate=False,
        )
        self.assertEqual(dry_result["landing_progress"], "cleanup_pending")
        self.assertEqual(dry_client.cleanup_calls, 0)

        failed_client = _OrdinaryLandingClient(
            cleanup=MergeTrainGitHubError("cleanup failed", status_code=503)
        )
        failed_result, _ = self._advance(
            record=terminal,
            landing_store=_LandingStore(terminal),
            client=failed_client,
        )
        self.assertEqual(failed_result["landing_progress"], "cleanup_pending")
        self.assertEqual(failed_result["candidate_ref_cleanup_status"], "failed")
        self.assertEqual(failed_client.cleanup_calls, 1)
        self.assertEqual(failed_client.candidate_ref_exists_calls, 0)

    def test_stale_second_entry_preserves_the_partial_successor(self) -> None:
        landing_store = _LandingStore(self.landing_record)
        first_result, _ = self._advance(
            record=self.landing_record,
            landing_store=landing_store,
            client=_OrdinaryLandingClient(),
        )
        self.assertEqual(first_result["landing_progress"], "partial")
        partial = landing_store.records[-1]
        record_count = len(landing_store.records)

        stale_result, _ = self._advance(
            record=partial,
            landing_store=landing_store,
            client=_OrdinaryLandingClient(stale=True),
        )
        self.assertEqual(stale_result["mode"], "blocked")
        self.assertEqual(stale_result["controller_reconciliation_status"], "required")
        error = stale_result["error"]
        self.assertIsInstance(error, dict)
        self.assertEqual(
            cast(dict[str, object], error)["code"], "ordinary_landing_recovery_required"
        )
        self.assertEqual(
            stale_result["merge_train_batch_landing_plan_record_id"], partial.record_id
        )
        self.assertEqual(len(landing_store.records), record_count)

    def test_ordinary_progress_requires_the_exact_persisted_record(self) -> None:
        landing_store = _NonReturningLandingStore(self.landing_record)
        with self.assertRaisesRegex(MergeTrainControllerRequestError, "exact progress record"):
            self._advance(
                record=self.landing_record,
                landing_store=landing_store,  # type: ignore[arg-type]
                client=_OrdinaryLandingClient(),
            )

    def test_ordinary_stack_collapse_is_rejected_before_landing_or_cleanup(self) -> None:
        client = _OrdinaryLandingClient()
        collapse_record = Mock()
        with (
            patch(
                "control_plane.merge_train_controller_run_once."
                "latest_merge_train_stack_collapse_plan_record_for_landing",
                return_value=collapse_record,
            ),
            patch(
                "control_plane.merge_train_controller_run_once."
                "validate_stack_collapse_record_for_landing"
            ),
        ):
            with self.assertRaisesRegex(MergeTrainControllerRequestError, "stack collapse"):
                self._advance(
                    record=self.landing_record,
                    landing_store=_LandingStore(self.landing_record),
                    client=client,
                )
        self.assertEqual(client.cleanup_calls, 0)

        terminal = self._terminal_record()
        controller_store = _ControllerStore(
            _controller_record(binding=self.binding, active_record_id=terminal.record_id)
        )
        lease = MergeTrainControllerLeaseContext(
            record=controller_store.record,
            record_store=controller_store,  # type: ignore[arg-type]
        )
        with patch(
            "control_plane.merge_train_controller_run_once."
            "latest_merge_train_stack_collapse_plan_record_for_completed_landing",
            return_value=collapse_record,
        ):
            with self.assertRaisesRegex(MergeTrainControllerRequestError, "stack collapse"):
                _finish_landed_merge_train_batch(
                    request=self.request,
                    policy_sha256=POLICY_SHA256,
                    repository_policy=self.policy,
                    trace_id="ordinary-landing-test",
                    recorded_at=NOW,
                    github_client=client,  # type: ignore[arg-type]
                    stack_collapse_store=_StackStore(),  # type: ignore[arg-type]
                    landed_record=terminal,
                    lease=lease,
                )
        self.assertEqual(client.cleanup_calls, 0)

    def _advance(
        self,
        *,
        record: MergeTrainBatchLandingPlanRecord,
        landing_store: _LandingStore,
        client: _OrdinaryLandingClient,
        mutate: bool = True,
        step_payload: dict[str, object] | None = None,
    ) -> tuple[dict[str, object], MergeTrainControllerLeaseContext]:
        controller_store = _ControllerStore(
            _controller_record(
                binding=self.binding,
                active_record_id=record.record_id,
                step_payload=step_payload,
            )
        )
        lease = MergeTrainControllerLeaseContext(
            record=controller_store.record,
            record_store=controller_store,  # type: ignore[arg-type]
        )
        request = self.request.model_copy(update={"mutate": mutate})
        result = _advance_active_landing_record(
            request=request,
            policy_sha256=POLICY_SHA256,
            repository_policy=self.policy,
            trace_id="ordinary-landing-test",
            recorded_at=NOW,
            github_client=client,  # type: ignore[arg-type]
            candidate_store=_CandidateStore(self.candidate_record),  # type: ignore[arg-type]
            landing_store=landing_store,
            stack_collapse_store=_StackStore(),  # type: ignore[arg-type]
            admission_store=Mock(),
            admission_evaluator=Mock(),
            active_landing_record=record,
            lease=lease,
        )
        return result, lease

    def _terminal_record(self) -> MergeTrainBatchLandingPlanRecord:
        entries = tuple(
            entry.model_copy(
                update={
                    "status": "merged",
                    "merge_commit_sha": f"merge-{entry.pull_request_number}",
                }
            )
            for entry in self.landing_record.landing_plan.entries
        )
        plan = self.landing_record.landing_plan.model_copy(update={"entries": entries})
        return build_merge_train_batch_landing_plan_record(
            ordinary_job_binding=self.binding,
            landing_plan=plan,
            source="test:terminal",
            updated_at=NOW,
        )


def _controller_record(
    *,
    binding: OrdinaryAgentJobBinding,
    active_record_id: str,
    step_payload: dict[str, object] | None = None,
) -> MergeTrainControllerStateRecord:
    return MergeTrainControllerStateRecord(
        ordinary_job_binding=binding,
        controller_key=build_merge_train_controller_key(repository=REPOSITORY, base_branch="main"),
        repository=REPOSITORY,
        base_branch="main",
        policy_key=f"{REPOSITORY}:main",
        policy_sha256=POLICY_SHA256,
        status="running",
        updated_at=NOW,
        lease_owner="ordinary-owner",
        lease_acquired_at=NOW,
        lease_expires_at="2026-09-09T12:05:00Z",
        heartbeat_at=NOW,
        active_action="land_batch",
        active_phase="merge_batch_entries",
        active_record_id=active_record_id,
        step_payload=step_payload or {},
    )


if __name__ == "__main__":
    unittest.main()
