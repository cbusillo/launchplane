from __future__ import annotations

import unittest

from sqlalchemy import select

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    build_ordinary_merge_train_candidate_ref,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
)
from control_plane.contracts.merge_train_effect import (
    MergeTrainEffectLineage,
    PullRequestHeadRefreshEffect,
)
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentClaimedJob,
    OrdinaryAgentControllerFence,
    OrdinaryAgentJobAttemptDisposition,
    PullRequestHeadRefreshCommand,
)
from control_plane.storage.postgres import (
    LaunchplaneMergeTrainBatchCandidateRow,
    LaunchplaneMergeTrainControllerStateRow,
    LaunchplaneOrdinaryAgentFiniteRequestRow,
    LaunchplaneOrdinaryAgentEffectRow,
    LaunchplaneOrdinaryAgentJobClaimRow,
)
from control_plane.ordinary_agent_session_lifecycle import (
    OrdinaryAgentSessionAdmissionDenied,
)
from tests import test_ordinary_agent_effect_storage as effect_support
from tests import test_ordinary_agent_session_storage as session_support
from tests.test_merge_admission_records import _guard_records


class OrdinaryAgentControllerCheckpointStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        session_fixture = session_support.OrdinaryAgentSessionStorageTests()
        session_fixture.setUp(pull_request_limit=2)
        self.addCleanup(session_fixture.doCleanups)
        self.session_fixture = session_fixture
        self.effect_fixture = effect_support.OrdinaryAgentEffectStorageTests()
        self.effect_fixture.prepare_effect_fixture(session_fixture)
        self.store = self.effect_fixture.store
        self.policy = self.effect_fixture.merge_policy
        self.request_a = self.effect_fixture.request
        self.request_b = self.store.admit_ordinary_agent_finite_request(
            proof=session_fixture.proof,
            request=session_fixture.request.model_copy(
                update={
                    "request_id": "finite-request-two",
                    "idempotency_key": "request-two",
                }
            ),
        )

    def _claim_and_acquire(
        self, *, worker_id: str, active_action: str = "head_refresh"
    ) -> tuple[
        OrdinaryAgentClaimedJob,
        MergeTrainControllerStateRecord,
        OrdinaryAgentControllerFence,
    ]:
        claim = self.store.claim_due_ordinary_agent_job(worker_id=worker_id, lease_seconds=30)
        assert claim is not None
        controller = self.store.acquire_ordinary_merge_train_controller_state_record(
            claim_fence=claim.claim_fence,
            expected_binding_revision=claim.request.binding_revision,
            policy_key=self.policy.policy.policies[0].policy_key,
            policy_sha256=self.policy.policy_sha256,
            lease_seconds=30,
            initial_active_action=active_action,
            initial_active_phase="prepare",
            adoptable_active_actions=(active_action,),
        )
        fence = OrdinaryAgentControllerFence(
            controller_key=controller.controller_key,
            lease_owner=controller.lease_owner,
            lease_acquired_at=controller.lease_acquired_at,
        )
        return claim, controller, fence

    def _seed_progress(
        self,
        *,
        controller: MergeTrainControllerStateRecord,
        record_id: str,
        step_payload: dict[str, object],
    ) -> MergeTrainBatchCandidateRecord:
        binding = controller.ordinary_job_binding
        assert binding is not None
        request = (
            self.request_a if binding.request_id == self.request_a.request_id else self.request_b
        )
        candidate, _, _, _ = _guard_records(
            repository=request.target.repository,
            pull_request_number=request.pull_requests[0].number,
            base_sha=request.base_sha,
            head_sha=request.pull_requests[0].head_sha,
            policy_sha256=self.policy.policy_sha256,
        )
        payload = candidate.model_dump(mode="json")
        payload["record_id"] = record_id
        payload["ordinary_job_binding"] = binding.model_dump(mode="json")
        payload["candidate"]["candidate_ref"] = build_ordinary_merge_train_candidate_ref(
            binding=binding, batch_id=candidate.candidate.batch_id
        )
        progress = MergeTrainBatchCandidateRecord.model_validate(payload)
        with self.store._session_factory() as session:
            session.add(
                LaunchplaneMergeTrainBatchCandidateRow(
                    record_id=progress.record_id,
                    status=progress.status,
                    source=progress.source,
                    updated_at=progress.updated_at,
                    repository=request.target.repository.lower(),
                    base_branch=request.target.base_branch,
                    batch_id=progress.candidate.batch_id,
                    candidate_status=progress.candidate.status,
                    payload=progress.model_dump(mode="json"),
                )
            )
            controller_row = session.get(
                LaunchplaneMergeTrainControllerStateRow, controller.controller_key
            )
            assert controller_row is not None
            current = MergeTrainControllerStateRecord.model_validate(controller_row.payload)
            controller_row.payload = current.model_copy(
                update={
                    "active_record_id": progress.record_id,
                    "active_phase": "checkpointed_phase",
                    "step_payload": step_payload,
                    "reconciliation_status": "adopted",
                    "reconciliation_detail": "server-derived-resume-detail",
                }
            ).model_dump(mode="json")
            session.commit()
        return progress

    def _yield(self, *, request_id: str, fence: OrdinaryAgentControllerFence) -> None:
        self.store.yield_ordinary_merge_train_controller_state_record(
            request_id=request_id,
            expected_binding_revision=1,
            controller_fence=fence,
        )

    def _finish_waiting(self, claim: OrdinaryAgentClaimedJob, *, due_offset: int = 100) -> None:
        self.store.finish_ordinary_agent_job_attempt(
            claim_fence=claim.claim_fence,
            disposition=OrdinaryAgentJobAttemptDisposition(
                status="waiting",
                next_due_at=self.session_fixture.now + due_offset,
                reason_code="controller_busy",
            ),
        )

    def _set_due_now(self, request_id: str) -> None:
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneOrdinaryAgentJobClaimRow, request_id)
            assert row is not None
            row.next_due_at = self.session_fixture.now
            session.commit()

    def test_two_parked_jobs_coexist_and_resume_restores_only_own_checkpoint(self) -> None:
        claim_a, controller_a, fence_a = self._claim_and_acquire(worker_id="worker-a")
        progress_a = self._seed_progress(
            controller=controller_a,
            record_id="candidate-record-a",
            step_payload={"owner": "a", "ordinal": 1},
        )
        self._yield(request_id=self.request_a.request_id, fence=fence_a)
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "job_still_dispatchable"):
            self.store.retire_ordinary_agent_job_history(claim_fence=claim_a.claim_fence)
        self._finish_waiting(claim_a)

        claim_b, controller_b, fence_b = self._claim_and_acquire(
            worker_id="worker-b", active_action="land_batch"
        )
        progress_b = self._seed_progress(
            controller=controller_b,
            record_id="candidate-record-b",
            step_payload={"owner": "b", "ordinal": 2},
        )
        self._yield(request_id=self.request_b.request_id, fence=fence_b)
        self._finish_waiting(claim_b)

        self._set_due_now(self.request_a.request_id)
        claim_a_again, resumed, resumed_fence = self._claim_and_acquire(worker_id="worker-a-next")
        self.assertEqual(claim_a_again.request.request_id, self.request_a.request_id)
        self.assertEqual(resumed.active_record_id, progress_a.record_id)
        self.assertEqual(resumed.active_action, controller_a.active_action)
        self.assertEqual(resumed.active_phase, "checkpointed_phase")
        self.assertEqual(resumed.step_payload, {"owner": "a", "ordinal": 1})
        self.assertEqual(resumed.reconciliation_status, "adopted")
        with self.store._session_factory() as session:
            claim_row = session.get(LaunchplaneOrdinaryAgentJobClaimRow, self.request_a.request_id)
            progress_b_row = session.get(
                LaunchplaneMergeTrainBatchCandidateRow, progress_b.record_id
            )
            assert claim_row is not None and progress_b_row is not None
            self.assertIsNone(claim_row.released_controller)
            self.assertEqual(progress_b_row.status, "active")
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "controller_binding_conflict"
        ):
            self._yield(request_id=self.request_a.request_id, fence=fence_a)
        current = self.store.list_merge_train_controller_state_records(
            repository=self.request_a.target.repository,
            base_branch=self.request_a.target.base_branch,
            limit=1,
        )[0]
        self.assertEqual(current.lease_owner, resumed_fence.lease_owner)
        self.assertEqual(current.active_record_id, progress_a.record_id)

    def test_completion_uses_own_checkpoint_while_foreign_controller_runs(self) -> None:
        claim_a, controller_a, fence_a = self._claim_and_acquire(worker_id="worker-a")
        progress_a = self._seed_progress(
            controller=controller_a,
            record_id="candidate-record-a",
            step_payload={"owner": "a"},
        )
        self._yield(request_id=self.request_a.request_id, fence=fence_a)
        self._finish_waiting(claim_a)
        _, controller_b, _ = self._claim_and_acquire(
            worker_id="worker-b", active_action="land_batch"
        )

        self._set_due_now(self.request_a.request_id)
        claim_a_again = self.store.claim_due_ordinary_agent_job(
            worker_id="worker-a-next", lease_seconds=30
        )
        assert claim_a_again is not None
        finished = self.store.finish_ordinary_agent_job_attempt(
            claim_fence=claim_a_again.claim_fence,
            disposition=OrdinaryAgentJobAttemptDisposition(status="completed"),
        )
        self.assertEqual(finished.status, "completed")
        with self.store._session_factory() as session:
            progress_row = session.get(LaunchplaneMergeTrainBatchCandidateRow, progress_a.record_id)
            controller_row = session.get(
                LaunchplaneMergeTrainControllerStateRow, controller_b.controller_key
            )
            claim_row = session.get(LaunchplaneOrdinaryAgentJobClaimRow, self.request_a.request_id)
            assert progress_row is not None and controller_row is not None and claim_row is not None
            self.assertEqual(progress_row.status, "superseded")
            self.assertEqual(
                MergeTrainControllerStateRecord.model_validate(
                    controller_row.payload
                ).ordinary_job_binding,
                controller_b.ordinary_job_binding,
            )
            self.assertIsNone(claim_row.released_controller)

    def test_unresolved_effect_and_rebound_checkpoint_each_block_foreign_acquire(self) -> None:
        claim_a, controller_a, fence_a = self._claim_and_acquire(worker_id="worker-a")
        self._seed_progress(
            controller=controller_a,
            record_id="candidate-record-a",
            step_payload={"owner": "a"},
        )
        command = PullRequestHeadRefreshCommand(
            effect=PullRequestHeadRefreshEffect(
                lineage=MergeTrainEffectLineage(
                    repository=self.request_a.target.repository,
                    base_branch=self.request_a.target.base_branch,
                ),
                pull_request_number=self.request_a.pull_requests[0].number,
                expected_head_sha=self.request_a.pull_requests[0].head_sha,
                expected_base_sha=self.request_a.base_sha,
            )
        )
        self.store.reserve_ordinary_agent_effect(
            request_id=self.request_a.request_id,
            expected_binding_revision=1,
            controller_fence=fence_a,
            command=command,
            semantic_ordinal=1,
        )
        with self.store._session_factory() as session:
            effect_row = session.scalar(
                select(LaunchplaneOrdinaryAgentEffectRow).where(
                    LaunchplaneOrdinaryAgentEffectRow.request_id == self.request_a.request_id
                )
            )
            assert effect_row is not None
            effect_row.payload = {
                **effect_row.payload,
                "target": {
                    **effect_row.payload["target"],
                    "repository": self.request_a.target.repository.upper(),
                },
            }
            session.commit()
        self._yield(request_id=self.request_a.request_id, fence=fence_a)
        self._finish_waiting(claim_a)
        claim_b = self.store.claim_due_ordinary_agent_job(worker_id="worker-b", lease_seconds=30)
        assert claim_b is not None
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "target_busy"):
            self.store.acquire_ordinary_merge_train_controller_state_record(
                claim_fence=claim_b.claim_fence,
                expected_binding_revision=1,
                policy_key=self.policy.policy.policies[0].policy_key,
                policy_sha256=self.policy.policy_sha256,
                lease_seconds=30,
                initial_active_action="head_refresh",
                initial_active_phase="prepare",
                adoptable_active_actions=("head_refresh",),
            )

        with self.store._session_factory() as session:
            effect_rows = tuple(
                session.scalars(
                    select(LaunchplaneOrdinaryAgentEffectRow).where(
                        LaunchplaneOrdinaryAgentEffectRow.request_id == self.request_a.request_id
                    )
                )
            )
            for effect_row in effect_rows:
                session.delete(effect_row)
            session.commit()
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "refresh_progress_must_be_retired"
        ):
            self.store.refresh_ordinary_agent_finite_job(
                request_id=self.request_a.request_id,
                expected_binding_revision=1,
                base_sha="c" * 40,
                pull_requests=self.request_a.pull_requests,
            )
        with self.store._session_factory() as session:
            request_row = session.get(
                LaunchplaneOrdinaryAgentFiniteRequestRow, self.request_a.request_id
            )
            assert request_row is not None
            current_request = type(self.request_a).model_validate(request_row.payload)
            self.assertEqual(current_request.binding_revision, 1)
            request_row.payload = self.request_a.model_copy(
                update={"binding_revision": 2}
            ).model_dump(mode="json")
            session.commit()
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "target_busy"):
            self.store.acquire_ordinary_merge_train_controller_state_record(
                claim_fence=claim_b.claim_fence,
                expected_binding_revision=1,
                policy_key=self.policy.policy.policies[0].policy_key,
                policy_sha256=self.policy.policy_sha256,
                lease_seconds=30,
                initial_active_action="head_refresh",
                initial_active_phase="prepare",
                adoptable_active_actions=("head_refresh",),
            )

    def test_terminal_yield_supersedes_tip_and_clears_checkpoint_and_shared_payload(self) -> None:
        _, controller_a, fence_a = self._claim_and_acquire(worker_id="worker-a")
        progress = self._seed_progress(
            controller=controller_a,
            record_id="candidate-record-a",
            step_payload={"owner": "a"},
        )
        with self.store._session_factory() as session:
            request_row = session.get(
                LaunchplaneOrdinaryAgentFiniteRequestRow, self.request_a.request_id
            )
            assert request_row is not None
            request_row.payload = self.request_a.model_copy(
                update={"status": "completed"}
            ).model_dump(mode="json")
            session.commit()
        self._yield(request_id=self.request_a.request_id, fence=fence_a)

        with self.store._session_factory() as session:
            claim_row = session.get(LaunchplaneOrdinaryAgentJobClaimRow, self.request_a.request_id)
            progress_row = session.get(LaunchplaneMergeTrainBatchCandidateRow, progress.record_id)
            controller_row = session.get(
                LaunchplaneMergeTrainControllerStateRow, controller_a.controller_key
            )
            assert claim_row is not None and progress_row is not None and controller_row is not None
            released = MergeTrainControllerStateRecord.model_validate(controller_row.payload)
            self.assertIsNone(claim_row.released_controller)
            self.assertEqual(progress_row.status, "superseded")
            self.assertEqual(released.step_payload, {})
            self.assertEqual(released.reconciliation_status, "clean")
            self.assertEqual(released.reconciliation_detail, "")


if __name__ == "__main__":
    unittest.main()
