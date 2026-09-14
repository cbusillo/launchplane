from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import ValidationError

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_landing_plan,
)
from control_plane.contracts.merge_train_controller_state import build_merge_train_controller_key
from control_plane.contracts.merge_train_historical_completion import (
    MergeTrainHistoricalCompletionEntryEvidence,
    MergeTrainHistoricalCompletionEvidence,
    MergeTrainHistoricalCompletionProviderEvidence,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentJobBinding
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import (
    LaunchplaneMergeTrainBatchLandingPlanRow,
    LaunchplaneOrdinaryAgentEffectRow,
    PostgresRecordStore,
)


def _landing_plan(*, stale: bool = False) -> MergeTrainBatchLandingPlan:
    candidate = MergeTrainBatchCandidate(
        batch_id="batch-example",
        repository="example/repository",
        base_branch="main",
        base_sha="base-sha",
        policy_key="example/repository:main",
        policy_sha256="a" * 64,
        candidate_ref="refs/heads/launchplane/train/example/repository/main/batch-example",
        candidate_sha="candidate-sha",
        candidate_tree_sha="candidate-tree",
        candidate_sha256="b" * 64,
        status="passed",
        entries=(
            MergeTrainBatchEntry(
                pull_request_number=17,
                position=1,
                head_sha="head-sha",
                head_tree_sha="head-tree",
            ),
        ),
        created_at="2026-09-14T01:00:00Z",
        updated_at="2026-09-14T01:00:00Z",
    )
    plan = build_merge_train_batch_landing_plan(
        candidate=candidate,
        merge_method="merge",
        created_at="2026-09-14T01:00:00Z",
    )
    if not stale:
        return plan
    payload = plan.model_dump(mode="python")
    payload["entries"] = tuple(
        entry.model_copy(update={"status": "stale"}) for entry in plan.entries
    )
    return MergeTrainBatchLandingPlan.model_validate(payload)


def _historical_completion(
    plan: MergeTrainBatchLandingPlan,
) -> MergeTrainHistoricalCompletionEvidence:
    entry = plan.entries[0]
    return MergeTrainHistoricalCompletionEvidence(
        classification="observed_merged_without_admission",
        authority_state="observation_only",
        source_landing_plan_record_id="merge-train-legacy-record",
        source_landing_plan_sha256=plan.landing_plan_sha256,
        controller_key=build_merge_train_controller_key(
            repository=plan.repository, base_branch=plan.base_branch
        ),
        repository=plan.repository,
        base_branch=plan.base_branch,
        landing_plan_id=plan.plan_id,
        batch_id=plan.batch_id,
        candidate_sha=plan.candidate_sha,
        candidate_sha256=plan.candidate_sha256,
        policy_key=plan.policy_key,
        policy_sha256=plan.policy_sha256,
        trace_id="launchplane_req_historical_1",
        provider_evidence=MergeTrainHistoricalCompletionProviderEvidence(
            observed_at="2026-09-14T01:25:52Z",
            observed_base_sha="observed-base",
            observed_base_tree_sha="observed-base-tree",
            final_observed_base_sha="observed-final-base",
            entries=(
                MergeTrainHistoricalCompletionEntryEvidence(
                    pull_request_number=entry.pull_request_number,
                    position=entry.position,
                    expected_head_sha=entry.expected_head_sha,
                    expected_head_tree_sha=entry.expected_head_tree_sha,
                    expected_base_sha=entry.expected_base_sha,
                    expected_parent_tree_sha="parent-tree",
                    expected_result_tree_sha="result-tree",
                    observed_head_sha=entry.expected_head_sha,
                    observed_head_tree_sha=entry.expected_head_tree_sha,
                    observed_merge_commit_sha="merge-commit",
                    observed_merge_commit_tree_sha="merge-commit-tree",
                    observed_parent_sha="observed-parent",
                    observed_parent_tree_sha="observed-parent-tree",
                    base_contains_merge_commit=True,
                ),
            ),
        ),
    )


def _record(
    *,
    plan: MergeTrainBatchLandingPlan,
    record_id: str = "merge-train-landing-record",
    schema_version: int = 1,
    ordinary: bool = False,
    historical: bool = False,
) -> MergeTrainBatchLandingPlanRecord:
    return MergeTrainBatchLandingPlanRecord(
        ordinary_job_binding=(
            OrdinaryAgentJobBinding(
                request_id="request-1",
                scope_sha256="c" * 64,
                binding_revision=1,
            )
            if ordinary
            else None
        ),
        schema_version=schema_version,
        record_id=record_id,
        status="active",
        source="test",
        updated_at="2026-09-14T01:30:00Z",
        landing_plan=plan,
        historical_completion=_historical_completion(plan) if historical else None,
    )


class HistoricalCompletionContractTests(unittest.TestCase):
    def test_normal_record_round_trip_payload_omits_none_and_observation_schema_is_valid(
        self,
    ) -> None:
        normal = _record(plan=_landing_plan())
        payload = normal.model_dump(mode="json", exclude_none=True)
        self.assertNotIn("historical_completion", payload)
        observation = _record(
            plan=_landing_plan(stale=True),
            record_id="merge-train-landing-observation",
            schema_version=2,
            historical=True,
        )
        historical_completion = observation.historical_completion
        self.assertIsNotNone(historical_completion)
        assert historical_completion is not None
        self.assertEqual(historical_completion.authority_state, "observation_only")
        self.assertEqual(observation.landing_plan.entries[0].status, "stale")

    def test_observation_rejects_schema_authority_state_ordinary_and_merged_shapes(self) -> None:
        with self.assertRaisesRegex(ValidationError, "schema_version 2"):
            _record(plan=_landing_plan(stale=True), historical=True)
        with self.assertRaisesRegex(ValidationError, "ordinary record"):
            _record(
                plan=_landing_plan(stale=True), schema_version=2, ordinary=True, historical=True
            )
        with self.assertRaisesRegex(ValidationError, "stale entries"):
            _record(plan=_landing_plan(), schema_version=2, historical=True)

        observation = _record(plan=_landing_plan(stale=True), schema_version=2, historical=True)
        payload = observation.model_dump(mode="python")
        payload["record_id"] = payload["historical_completion"]["source_landing_plan_record_id"]
        with self.assertRaisesRegex(ValidationError, "source record"):
            MergeTrainBatchLandingPlanRecord.model_validate(payload)

        payload = observation.model_dump(mode="python")
        payload["historical_completion"]["provider_evidence"]["entries"][0]["expected_head_sha"] = (
            "different-head"
        )
        with self.assertRaisesRegex(ValidationError, "entry identity"):
            MergeTrainBatchLandingPlanRecord.model_validate(payload)

    def test_filesystem_rejects_observation_creation_and_overwrite(self) -> None:
        normal = _record(plan=_landing_plan())
        observation = _record(
            plan=_landing_plan(stale=True),
            record_id="merge-train-landing-observation",
            schema_version=2,
            historical=True,
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_batch_landing_plan_record(normal)
            loaded = store.list_merge_train_batch_landing_plan_records()[0]
            self.assertIsNone(loaded.historical_completion)
            with self.assertRaisesRegex(ValueError, "observation-only"):
                store.write_merge_train_batch_landing_plan_record(observation)

            record_path = (
                Path(directory)
                / "launchplane_merge_train_batch_landing_plans"
                / f"{observation.record_id}.json"
            )
            record_path.parent.mkdir(parents=True, exist_ok=True)
            record_path.write_text(
                json.dumps(observation.model_dump(mode="json", exclude_none=True)),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "observation-only"):
                store.write_merge_train_batch_landing_plan_record(
                    observation.model_copy(update={"historical_completion": None})
                )

    def test_postgres_sqlite_rejects_observation_creation_and_overwrite(self) -> None:
        normal = _record(plan=_landing_plan())
        observation = _record(
            plan=_landing_plan(stale=True),
            record_id="merge-train-landing-observation",
            schema_version=2,
            historical=True,
        )
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'records.sqlite3'}"
            )
            store.ensure_schema()
            store.write_merge_train_batch_landing_plan_record(normal)
            self.assertIsNone(
                store.list_merge_train_batch_landing_plan_records()[0].historical_completion
            )
            with self.assertRaisesRegex(ValueError, "observation-only"):
                store.write_merge_train_batch_landing_plan_record(observation)

            with store._session_factory() as session:
                session.merge(
                    LaunchplaneMergeTrainBatchLandingPlanRow(
                        record_id=observation.record_id,
                        status=observation.status,
                        source=observation.source,
                        updated_at=observation.updated_at,
                        repository=observation.landing_plan.repository,
                        base_branch=observation.landing_plan.base_branch,
                        batch_id=observation.landing_plan.batch_id,
                        plan_id=observation.landing_plan.plan_id,
                        payload=observation.model_dump(mode="json", exclude_none=True),
                    )
                )
                session.commit()
            with self.assertRaisesRegex(ValueError, "overwritten"):
                store.write_merge_train_batch_landing_plan_record(
                    observation.model_copy(update={"historical_completion": None})
                )
            store.close()

    def test_fence_reader_is_bounded_and_checks_active_ordinary_state(self) -> None:
        normal = _record(plan=_landing_plan(), ordinary=True)
        with TemporaryDirectory() as directory:
            filesystem_store = FilesystemRecordStore(Path(directory))
            self.assertFalse(
                filesystem_store.has_ordinary_merge_train_target_fence(
                    repository="example/repository", base_branch="main"
                )
            )
            filesystem_store.write_merge_train_batch_landing_plan_record(normal)
            self.assertTrue(
                filesystem_store.has_ordinary_merge_train_target_fence(
                    repository="EXAMPLE/REPOSITORY", base_branch="main"
                )
            )

            postgres_store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'fence.sqlite3'}"
            )
            postgres_store.ensure_schema()
            with postgres_store._session_factory() as session:
                session.merge(
                    LaunchplaneMergeTrainBatchLandingPlanRow(
                        record_id=normal.record_id,
                        status="active",
                        source="test",
                        updated_at=normal.updated_at,
                        repository="example/repository",
                        base_branch="main",
                        batch_id=normal.landing_plan.batch_id,
                        plan_id=normal.landing_plan.plan_id,
                        payload=normal.model_dump(mode="json", exclude_none=True),
                    )
                )
                session.commit()
            self.assertTrue(
                postgres_store.has_ordinary_merge_train_target_fence(
                    repository="example/repository", base_branch="main"
                )
            )
            postgres_store.close()

            effect_store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'effect.sqlite3'}"
            )
            effect_store.ensure_schema()
            with effect_store._session_factory() as session:
                session.merge(
                    LaunchplaneOrdinaryAgentEffectRow(
                        effect_id="effect-1",
                        lease_id="lease-1",
                        request_id="request-1",
                        scope_sha256="d" * 64,
                        binding_revision=1,
                        action_ordinal=1,
                        semantic_key="merge-train",
                        command_sha256="e" * 64,
                        revision=1,
                        payload={
                            "target": {
                                "repository": "example/repository",
                                "base_branch": "main",
                            },
                            "state": "reserved",
                        },
                    )
                )
                session.commit()
            self.assertTrue(
                effect_store.has_ordinary_merge_train_target_fence(
                    repository="example/repository", base_branch="main"
                )
            )
            effect_store.close()
