import unittest
from unittest.mock import Mock

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchEntry,
    build_merge_train_batch_candidate_record,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerLeaseLostError,
    build_merge_train_controller_state_record,
)
from control_plane.contracts.ordinary_agent import OrdinaryAgentPullRequest
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentClaimedJob,
    OrdinaryAgentControllerFence,
    OrdinaryAgentControllerStore,
    OrdinaryAgentJobClaimFence,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
)
from control_plane.merge_train_controller_run_once import MergeTrainControllerLeaseContext
from control_plane.ordinary_agent_controller_store import (
    OrdinaryAgentControllerAdapter,
    OrdinaryAgentControllerReadStore,
    OrdinaryAgentProgressAdapter,
    OrdinaryAgentProgressReadStore,
)
from tests.support.ordinary_agent_lifecycle import TARGET


class OrdinaryAgentControllerStoreTests(unittest.TestCase):
    def test_successor_pointer_survives_legacy_checkpoint_and_cancel_releases_without_acquire(
        self,
    ) -> None:
        request = OrdinaryAgentFiniteRequestRecord(
            request_id="job-one",
            idempotency_key="job-one",
            principal_id="agent_one",
            session_id="session_one",
            lease_id="lease_one",
            target=TARGET,
            base_sha="a" * 40,
            pull_requests=(OrdinaryAgentPullRequest(number=12, head_sha="b" * 40),),
            permitted_stack_edit_pull_requests=(),
            refresh_allowance_total=0,
            admitted_at=10,
            expires_at=100,
        )
        claim = OrdinaryAgentClaimedJob(
            request=request,
            claim_fence=OrdinaryAgentJobClaimFence(
                request_id=request.request_id, worker_id="worker", generation=1
            ),
            claim_expires_at=50,
        )
        store = Mock(spec=OrdinaryAgentControllerStore)
        reader = Mock(spec=OrdinaryAgentControllerReadStore)
        adapter = OrdinaryAgentControllerAdapter(claim, store, reader)
        record = build_merge_train_controller_state_record(
            repository=TARGET.repository,
            base_branch=TARGET.base_branch,
            policy_key="test-policy",
            policy_sha256="c" * 64,
            updated_at="2026-01-01T00:00:00Z",
        ).model_copy(
            update={
                "ordinary_job_binding": adapter.binding,
                "status": "running",
                "lease_owner": "server-derived-owner",
                "lease_acquired_at": "2026-01-01T00:00:00Z",
                "active_record_id": "predecessor",
            }
        )
        store.acquire_ordinary_merge_train_controller_state_record.return_value = record
        adapter.acquire_merge_train_controller_state_record(
            repository=TARGET.repository,
            base_branch=TARGET.base_branch,
            policy_key=record.policy_key,
            policy_sha256=record.policy_sha256,
            lease_owner="untrusted-generic-trace",
            lease_seconds=60,
            initial_active_action="build_candidate",
            initial_active_phase="planning",
            adoptable_active_actions=("build_candidate",),
        )
        store.acquire_ordinary_merge_train_controller_state_record.reset_mock()
        progress_reader = Mock(spec=OrdinaryAgentProgressReadStore)
        progress = OrdinaryAgentProgressAdapter(adapter, progress_reader)
        candidate = MergeTrainBatchCandidate(
            batch_id="batch-one",
            repository=TARGET.repository,
            base_branch=TARGET.base_branch,
            base_sha=request.base_sha,
            policy_key=record.policy_key,
            policy_sha256=record.policy_sha256,
            candidate_ref="refs/heads/test-candidate",
            entries=(MergeTrainBatchEntry(pull_request_number=12, position=1, head_sha="b" * 40),),
            created_at=record.updated_at,
            updated_at=record.updated_at,
        )
        wrapper = build_merge_train_batch_candidate_record(
            candidate=candidate,
            source="test",
            updated_at=record.updated_at,
            ordinary_job_binding=adapter.binding,
        )
        progress_reader.list_merge_train_batch_candidate_records.return_value = (
            wrapper.model_copy(update={"ordinary_job_binding": None}),
            wrapper.model_copy(
                update={
                    "ordinary_job_binding": adapter.binding.model_copy(
                        update={"binding_revision": 2}
                    )
                }
            ),
            wrapper,
        )
        self.assertEqual(
            progress.list_merge_train_batch_candidate_records(
                repository=TARGET.repository, base_branch=TARGET.base_branch, limit=1
            ),
            (wrapper,),
        )
        reader.list_merge_train_controller_state_records.return_value = (record,)
        progress.write_merge_train_batch_candidate_record(wrapper)
        written = store.write_ordinary_merge_train_record.call_args.kwargs
        self.assertEqual(written["controller_fence"], adapter.acquired_fence)
        self.assertEqual(written["expected_predecessor_record_id"], "predecessor")
        self.assertEqual(written["record"], wrapper)
        successor = record.model_copy(update={"active_record_id": "successor"})
        foreign = record.model_copy(
            update={
                "ordinary_job_binding": adapter.binding.model_copy(
                    update={"request_id": "foreign-job"}
                )
            }
        )
        reader.list_merge_train_controller_state_records.return_value = (foreign, successor)
        store.compare_and_set_ordinary_merge_train_controller_state_record.side_effect = (
            lambda **kwargs: kwargs["record"]
        )
        lease = MergeTrainControllerLeaseContext(record=record, record_store=adapter)
        result = lease.checkpoint(active_phase="candidate_result_recorded")
        self.assertEqual(result.active_record_id, "successor")
        self.assertEqual(result.active_phase, "candidate_result_recorded")
        changed = successor.model_copy(update={"lease_owner": "replacement-worker"})
        reader.list_merge_train_controller_state_records.return_value = (changed,)
        with self.assertRaises(MergeTrainControllerLeaseLostError):
            lease.checkpoint(active_phase="must-not-run")
        with self.assertRaises(MergeTrainControllerLeaseLostError):
            progress.write_merge_train_batch_candidate_record(wrapper)
        self.assertEqual(store.write_ordinary_merge_train_record.call_count, 1)
        self.assertEqual(
            store.compare_and_set_ordinary_merge_train_controller_state_record.call_count, 1
        )
        # Cancellation may make all current-authority reads unusable. Release
        # still uses its exact persisted fence and carries no raw exception text.
        reader.list_merge_train_controller_state_records.side_effect = PermissionError()
        store.yield_ordinary_merge_train_controller_state_record.return_value = record.model_copy(
            update={"status": "idle", "lease_owner": "", "lease_acquired_at": ""}
        )
        lease.release(
            reconciliation_status="required", reconciliation_detail="private-error-sentinel"
        )
        fence = OrdinaryAgentControllerFence(
            controller_key=record.controller_key,
            lease_owner=record.lease_owner,
            lease_acquired_at=record.lease_acquired_at,
        )
        store.yield_ordinary_merge_train_controller_state_record.assert_called_once_with(
            request_id=request.request_id,
            expected_binding_revision=request.binding_revision,
            controller_fence=fence,
        )
        terminal = OrdinaryAgentControllerAdapter(
            claim.model_copy(
                update={
                    "request": request.model_copy(update={"status": "cancelled"}),
                    "controller_fence": fence,
                }
            ),
            store,
            reader,
        )
        terminal.release_terminal_history()
        store.acquire_ordinary_merge_train_controller_state_record.assert_not_called()
        self.assertNotIn("private-error-sentinel", repr(store.mock_calls))
