"""Atomic ordinary-agent readmission from exact persisted provider evidence."""

from __future__ import annotations

import unittest

from sqlalchemy import select

from control_plane.contracts import ordinary_agent_snapshot as snapshots
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentControllerFence,
    OrdinaryAgentJobAttemptDisposition,
    OrdinaryAgentSnapshotAttemptRecord,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentLeaseRecord,
)
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.storage.postgres import (
    LaunchplaneOrdinaryAgentEffectRow,
    LaunchplaneOrdinaryAgentFiniteRequestRow,
    LaunchplaneOrdinaryAgentLandingPreparationRow,
    LaunchplaneOrdinaryAgentLeaseRow,
    LaunchplaneOrdinaryAgentReadAttemptRow,
)
from tests import test_ordinary_agent_effect_storage as effect_support
from tests import test_ordinary_agent_session_storage as session_support


class ReadmissionStorageFixture:
    def __init__(
        self,
        test: unittest.TestCase,
        session_fixture: session_support.OrdinaryAgentSessionStorageTests,
    ) -> None:
        self.test = test
        self.effect = effect_support.OrdinaryAgentEffectStorageTests()
        self.effect.prepare_effect_fixture(session_fixture)
        self.store = self.effect.store
        self.request = self.effect.request

    def observation(self) -> snapshots.OrdinaryAgentReadmissionObservation:
        payload = {
            "schema_version": 1,
            "observed_at": self.effect.fixture.now,
            "target": self.request.target.model_dump(mode="json"),
            "repository_owner_id": 202,
            "captured_base_sha": self.request.base_sha,
            "captured_pull_requests": [
                item.model_dump(mode="json") for item in self.request.pull_requests
            ],
            "base_identity": {
                "sha": "e" * 40,
                "tree_sha": "f" * 40,
                "parent_shas": [self.request.base_sha],
            },
            "pull_requests": [
                {
                    "number": item.number,
                    "head_sha": "d" * 40,
                    "base_ref": self.request.target.base_branch,
                    "lifecycle": "closed",
                    "head_repository_id": self.request.target.repository_id,
                    "head_repository": self.request.target.repository,
                    "base_repository_id": self.request.target.repository_id,
                    "base_repository": self.request.target.repository,
                }
                for item in self.request.pull_requests
            ],
            "drift": "base_and_head",
            "counts": {"rest_core_requests": 3, "graphql_requests": 1, "graphql_points": 2},
        }
        return snapshots.OrdinaryAgentReadmissionObservation.model_validate(
            {**payload, "observation_sha256": canonical_json_sha256(payload)}
        )

    def observe(
        self, *, close_custody: bool = True, yield_controller: bool = True
    ) -> tuple[
        OrdinaryAgentControllerFence,
        OrdinaryAgentSnapshotAttemptRecord,
        snapshots.OrdinaryAgentReadmissionObservation,
        str,
    ]:
        fence, self.command = self.effect.prepare_controller()
        attempt = self.store.reserve_ordinary_agent_snapshot_attempt(
            request_id=self.request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
        )
        custody = self.store.reserve_ordinary_agent_read_custody_attempt(
            attempt_id=attempt.attempt_id,
            expected_attempt_revision=attempt.revision,
        )
        self.effect.issue(custody)
        observation = self.observation()
        completed = self.store.record_ordinary_agent_snapshot_success(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=custody.custody_attempt_id,
            result=observation,
        )
        if close_custody:
            self.store.close_ordinary_agent_custody_issue_attempt(
                attempt_id=custody.custody_attempt_id,
                reason="confirmed_revoked",
            )
        if yield_controller:
            self.store.yield_ordinary_merge_train_controller_state_record(
                request_id=self.request.request_id,
                expected_binding_revision=1,
                controller_fence=fence,
            )
        return fence, completed, observation, custody.custody_attempt_id

    def finalize(
        self,
        *,
        attempt: OrdinaryAgentSnapshotAttemptRecord,
        observation: snapshots.OrdinaryAgentReadmissionObservation,
    ) -> OrdinaryAgentFiniteRequestRecord:
        return self.store.finalize_ordinary_agent_readmission(
            claim_fence=self.effect.claim.claim_fence,
            expected_binding_revision=1,
            read_attempt_id=attempt.attempt_id,
            expected_observation_sha256=observation.observation_sha256,
        )


class OrdinaryAgentReadmissionStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        session_fixture = session_support.OrdinaryAgentSessionStorageTests()
        session_fixture.setUp()
        self.addCleanup(session_fixture.doCleanups)
        self.fixture = ReadmissionStorageFixture(self, session_fixture)
        self.store = self.fixture.store

    def test_readmission_replay_waits_for_cleanup_and_new_fence_gets_fresh_attempt(self) -> None:
        fence, completed, observation, custody_id = self.fixture.observe(
            close_custody=False, yield_controller=False
        )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "read_custody_fenced"):
            self.store.reserve_ordinary_agent_snapshot_attempt(
                request_id=self.fixture.request.request_id,
                expected_binding_revision=1,
                controller_fence=fence,
            )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=custody_id, reason="confirmed_revoked"
        )
        replay = self.store.reserve_ordinary_agent_snapshot_attempt(
            request_id=self.fixture.request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
        )
        self.assertEqual(replay, completed)
        self.assertEqual(replay.result, observation)

        self.store.yield_ordinary_merge_train_controller_state_record(
            request_id=self.fixture.request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
        )
        self.store.finish_ordinary_agent_job_attempt(
            claim_fence=self.fixture.effect.claim.claim_fence,
            disposition=OrdinaryAgentJobAttemptDisposition(
                status="waiting", next_due_at=self.fixture.effect.fixture.now
            ),
        )
        next_claim = self.store.claim_due_ordinary_agent_job(
            worker_id="readmission-next-fence", lease_seconds=30
        )
        assert next_claim is not None
        controller = self.store.acquire_ordinary_merge_train_controller_state_record(
            claim_fence=next_claim.claim_fence,
            expected_binding_revision=1,
            policy_key=self.fixture.effect.merge_policy.policy.policies[0].policy_key,
            policy_sha256=self.fixture.effect.merge_policy.policy_sha256,
            lease_seconds=30,
            initial_active_action="snapshot",
            initial_active_phase="read",
            adoptable_active_actions=("snapshot",),
        )
        next_fence = OrdinaryAgentControllerFence(
            controller_key=controller.controller_key,
            lease_owner=controller.lease_owner,
            lease_acquired_at=controller.lease_acquired_at,
        )
        fresh = self.store.reserve_ordinary_agent_snapshot_attempt(
            request_id=self.fixture.request.request_id,
            expected_binding_revision=1,
            controller_fence=next_fence,
        )
        self.assertNotEqual(fresh.attempt_id, completed.attempt_id)
        self.assertEqual(fresh.attempt_ordinal, completed.attempt_ordinal + 1)

    def test_finalize_atomically_rebinds_then_replays_without_mutating_later_controller(
        self,
    ) -> None:
        _, completed, observation, _ = self.fixture.observe()
        with self.store._session_factory() as session:
            lease_row = session.get(LaunchplaneOrdinaryAgentLeaseRow, self.fixture.request.lease_id)
            assert lease_row is not None
            budget_before = OrdinaryAgentLeaseRecord.model_validate(lease_row.payload).budget

        rebound = self.fixture.finalize(attempt=completed, observation=observation)
        self.assertEqual(rebound.binding_revision, 2)
        self.assertEqual(rebound.refresh_used, 1)
        self.assertEqual(rebound.base_sha, observation.base_identity.sha)
        self.assertEqual(
            tuple(item.head_sha for item in rebound.pull_requests),
            tuple(item.head_sha for item in observation.pull_requests),
        )
        self.assertEqual(rebound.execution_record_ids, ())
        with self.store._session_factory() as session:
            attempt_row = session.get(LaunchplaneOrdinaryAgentReadAttemptRow, completed.attempt_id)
            lease_row = session.get(LaunchplaneOrdinaryAgentLeaseRow, self.fixture.request.lease_id)
            assert attempt_row is not None and lease_row is not None
            consumed = OrdinaryAgentSnapshotAttemptRecord.model_validate(attempt_row.payload)
            self.assertEqual((consumed.state, consumed.rebound_revision), ("consumed", 2))
            self.assertEqual(
                OrdinaryAgentLeaseRecord.model_validate(lease_row.payload).budget, budget_before
            )
            self.assertEqual(
                tuple(
                    session.scalars(
                        select(LaunchplaneOrdinaryAgentEffectRow.effect_id).where(
                            LaunchplaneOrdinaryAgentEffectRow.request_id == rebound.request_id
                        )
                    )
                ),
                (),
            )
            self.assertEqual(
                tuple(
                    session.scalars(
                        select(LaunchplaneOrdinaryAgentLandingPreparationRow.preparation_id).where(
                            LaunchplaneOrdinaryAgentLandingPreparationRow.request_id
                            == rebound.request_id
                        )
                    )
                ),
                (),
            )

        self.store.finish_ordinary_agent_job_attempt(
            claim_fence=self.fixture.effect.claim.claim_fence,
            disposition=OrdinaryAgentJobAttemptDisposition(
                status="waiting", next_due_at=self.fixture.effect.fixture.now
            ),
        )
        next_claim = self.store.claim_due_ordinary_agent_job(
            worker_id="readmission-later-controller", lease_seconds=30
        )
        assert next_claim is not None
        later_controller = self.store.acquire_ordinary_merge_train_controller_state_record(
            claim_fence=next_claim.claim_fence,
            expected_binding_revision=2,
            policy_key=self.fixture.effect.merge_policy.policy.policies[0].policy_key,
            policy_sha256=self.fixture.effect.merge_policy.policy_sha256,
            lease_seconds=30,
            initial_active_action="snapshot",
            initial_active_phase="read",
            adoptable_active_actions=("snapshot",),
        )
        replay = self.fixture.finalize(attempt=completed, observation=observation)
        self.assertEqual(replay, rebound)
        self.assertEqual(
            self.store.list_merge_train_controller_state_records(
                repository=rebound.target.repository,
                base_branch=rebound.target.base_branch,
                limit=1,
            )[0],
            later_controller,
        )

    def test_finalize_requires_yield_exact_claim_and_available_refresh_budget(self) -> None:
        _, completed, observation, _ = self.fixture.observe(yield_controller=False)
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "refresh_progress_must_be_retired"
        ):
            self.fixture.finalize(attempt=completed, observation=observation)
        with self.store._session_factory() as session:
            request_row = session.get(
                LaunchplaneOrdinaryAgentFiniteRequestRow, self.fixture.request.request_id
            )
            attempt_row = session.get(LaunchplaneOrdinaryAgentReadAttemptRow, completed.attempt_id)
            assert request_row is not None and attempt_row is not None
            self.assertEqual(request_row.payload["binding_revision"], 1)
            self.assertEqual(attempt_row.state, "completed")

        self.store.yield_ordinary_merge_train_controller_state_record(
            request_id=self.fixture.request.request_id,
            expected_binding_revision=1,
            controller_fence=completed.controller_fence,
        )
        wrong_claim = self.fixture.effect.claim.claim_fence.model_copy(
            update={"request_id": "another-request"}
        )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "readmission_fence_conflict"
        ):
            self.store.finalize_ordinary_agent_readmission(
                claim_fence=wrong_claim,
                expected_binding_revision=1,
                read_attempt_id=completed.attempt_id,
                expected_observation_sha256=observation.observation_sha256,
            )

        with self.store._session_factory() as session:
            request_row = session.get(
                LaunchplaneOrdinaryAgentFiniteRequestRow, self.fixture.request.request_id
            )
            assert request_row is not None
            request = OrdinaryAgentFiniteRequestRecord.model_validate(request_row.payload)
            request_row.payload = request.model_copy(update={"refresh_used": 1}).model_dump(
                mode="json", exclude_none=True
            )
            session.commit()
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "refresh_allowance_exhausted"
        ):
            self.fixture.finalize(attempt=completed, observation=observation)

    def test_finalize_rejects_real_effect_without_consuming_observation(self) -> None:
        _, completed, observation, _ = self.fixture.observe(yield_controller=False)
        self.store.reserve_ordinary_agent_effect(
            request_id=self.fixture.request.request_id,
            expected_binding_revision=1,
            controller_fence=completed.controller_fence,
            command=self.fixture.command,
            semantic_ordinal=1,
        )
        self.store.yield_ordinary_merge_train_controller_state_record(
            request_id=self.fixture.request.request_id,
            expected_binding_revision=1,
            controller_fence=completed.controller_fence,
        )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "refresh_progress_must_be_retired"
        ):
            self.fixture.finalize(attempt=completed, observation=observation)
        with self.store._session_factory() as session:
            request_row = session.get(
                LaunchplaneOrdinaryAgentFiniteRequestRow, self.fixture.request.request_id
            )
            attempt_row = session.get(LaunchplaneOrdinaryAgentReadAttemptRow, completed.attempt_id)
            assert request_row is not None and attempt_row is not None
            self.assertEqual(request_row.payload["binding_revision"], 1)
            self.assertEqual(attempt_row.state, "completed")
