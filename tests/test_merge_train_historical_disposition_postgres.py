from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
from typing import Iterator
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch

from sqlalchemy import func, select, text
from sqlalchemy.orm import object_session

from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.merge_train_historical_completion import (
    MergeTrainHistoricalCompletionProviderEvidence,
)
from control_plane.contracts.merge_admission_record import (
    MergeAdmissionRecord,
    build_merge_effect_attempt_id,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_candidate_record,
    build_merge_train_batch_candidate_ref,
    build_merge_train_batch_landing_plan,
    build_merge_train_batch_landing_plan_record,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_train_effect import (
    MergeTrainEffectLineage,
    StackChildLabelEffect,
)
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentControllerFence,
    OrdinaryAgentEffectRecord,
    StackChildLabelCommand,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapseEntry,
    MergeTrainStackCollapseMutation,
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.merge_train_historical_completion import (
    assess_merge_train_historical_completion,
)
from control_plane.merge_train_historical_disposition import (
    HistoricalDispositionError,
    HistoricalDispositionRequest,
)
from control_plane.workflows.merge_train_controller import (
    decide_merge_train_controller_record_action,
)
from control_plane.storage.historical_completion import _all_target_admissions
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.merge_train_github import GitHubMergeTrainClient
from control_plane.service_auth import LaunchplaneAuthzPolicy, LocalOperatorIdentity
from control_plane.storage.postgres import (
    LaunchplaneMergeAdmissionRow,
    LaunchplaneMergeTrainBatchCandidateRow,
    LaunchplaneMergeTrainBatchLandingPlanRow,
    LaunchplaneMergeTrainStackCollapsePlanRow,
    LaunchplaneMergeTrainControllerStateRow,
    LaunchplaneOrdinaryAgentEffectRow,
    PostgresRecordStore,
)
from tests.support.auth import _local_operator_policy
from tests.test_merge_train_historical_completion import (
    BASE_BRANCH,
    OBSERVED_AT,
    REPOSITORY,
    _HistoricalCompletionFixture,
    _ReadOnlyTransport,
    _provider_responses,
    seed_crowded_scoped_history,
)
from tests.test_postgres_integration import _store_for_fresh_head_database
from tests.test_merge_readiness import (
    _candidate as _readiness_candidate,
    _evaluate as _evaluate_readiness,
    _fence as _readiness_fence,
    _merge_admission,
    _target as _readiness_target,
)


IDENTITY = LocalOperatorIdentity(subject="local-owner-agent", token_label="local-owner-write")
ROUTE = "/v1/work-graph/merge-train/controller/run-once"


def _authz_policy_record(*, allow: bool = True, revision: int = 1) -> LaunchplaneAuthzPolicyRecord:
    policy = (
        _local_operator_policy(
            actions=("merge_train.run_once",),
            products=("launchplane",),
            contexts=("launchplane",),
        )
        if allow
        else LaunchplaneAuthzPolicy(local_operators=())
    )
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=revision, policy_sha256=digest),
        revision=revision,
        status="active",
        source="test:historical-disposition",
        updated_at=f"2026-09-13T12:00:0{revision}Z",
        policy_sha256=digest,
        policy=policy,
    )


def _seed_fixture(store: PostgresRecordStore, fixture: _HistoricalCompletionFixture) -> None:
    store.write_merge_train_policy_record(fixture.policy_record)
    store.write_merge_train_batch_candidate_record(fixture.candidate_record)
    store.write_merge_train_batch_landing_plan_record(fixture.landing_record)
    store.write_merge_train_controller_state_record(fixture.controller)
    store.seed_authz_policy_if_absent(_authz_policy_record())


def _request(
    fixture: _HistoricalCompletionFixture, *, key: str = "history-1"
) -> HistoricalDispositionRequest:
    return HistoricalDispositionRequest(
        repository=REPOSITORY,
        base_branch=BASE_BRANCH,
        selector=fixture.selector,
        identity=IDENTITY,
        scope=idempotency_scope(IDENTITY),
        route_path=ROUTE,
        idempotency_key=key,
        request_fingerprint=f"fingerprint-{key}",
    )


@contextmanager
def _prepared_store() -> Iterator[tuple[PostgresRecordStore, _HistoricalCompletionFixture]]:
    with _store_for_fresh_head_database() as store, TemporaryDirectory() as directory:
        fixture = _HistoricalCompletionFixture(Path(directory))
        _seed_fixture(store, fixture)
        yield store, fixture


def _provider_evidence(
    store: PostgresRecordStore,
    fixture: _HistoricalCompletionFixture,
    *,
    transport: _ReadOnlyTransport | None = None,
) -> MergeTrainHistoricalCompletionProviderEvidence:
    provider_transport = transport or _ReadOnlyTransport(responses=_provider_responses())
    result = assess_merge_train_historical_completion(
        store=store,
        repository=REPOSITORY,
        base_branch=BASE_BRANCH,
        selector=fixture.selector,
        generated_at=OBSERVED_AT,
        github_client=GitHubMergeTrainClient(transport=provider_transport),
    )
    assert result.provider_evidence is not None
    return result.provider_evidence


def _stack_member_record(
    fixture: _HistoricalCompletionFixture,
) -> MergeTrainStackCollapsePlanRecord:
    plan = MergeTrainStackCollapsePlan(
        collapse_id="member-collapse",
        repository=REPOSITORY,
        base_branch=BASE_BRANCH,
        root_pull_request_number=900,
        root_initial_head_sha="a" * 40,
        root_head_ref="refs/heads/member-root",
        policy_key=fixture.policy_record.policy.policies[0].policy_key,
        policy_sha256=fixture.policy_record.policy_sha256,
        entries=(
            MergeTrainStackCollapseEntry(
                pull_request_number=900,
                position=1,
                head_sha="a" * 40,
                head_ref="refs/heads/member-root",
                base_ref=BASE_BRANCH,
            ),
            MergeTrainStackCollapseEntry(
                pull_request_number=1,
                position=2,
                head_sha="b" * 40,
                head_ref="refs/heads/member-child",
                base_ref=BASE_BRANCH,
            ),
        ),
        mutations=(
            MergeTrainStackCollapseMutation(
                child_pull_request_number=1,
                parent_pull_request_number=900,
                child_head_sha="b" * 40,
                expected_parent_head_sha="a" * 40,
                parent_head_ref="refs/heads/member-root",
            ),
        ),
        created_at=OBSERVED_AT,
        updated_at=OBSERVED_AT,
    )
    return MergeTrainStackCollapsePlanRecord(
        record_id="member-stack-record",
        source="test:historical-disposition",
        updated_at=OBSERVED_AT,
        plan=plan,
    )


def _ordinary_effect_record(
    fixture: _HistoricalCompletionFixture, *, state: str
) -> OrdinaryAgentEffectRecord:
    command = StackChildLabelCommand(
        effect=StackChildLabelEffect(
            lineage=MergeTrainEffectLineage(repository=REPOSITORY, base_branch=BASE_BRANCH),
            pull_request_number=1,
            label="test-label",
        )
    )
    return OrdinaryAgentEffectRecord(
        effect_id=f"effect-{state}",
        request_id="ordinary-request",
        session_id="ordinary-session",
        lease_id="ordinary-lease",
        principal_id="ordinary-principal",
        scope_sha256="a" * 64,
        binding_revision=1,
        semantic_ordinal=1,
        action_ordinal=1,
        command_sha256=canonical_json_sha256(command.model_dump(mode="json")),
        command=command,
        target=OrdinaryAgentTarget(repository_id=1, repository=REPOSITORY, base_branch=BASE_BRANCH),
        controller_fence=OrdinaryAgentControllerFence(
            controller_key=fixture.controller.controller_key,
            lease_owner="ordinary-owner",
            lease_acquired_at=OBSERVED_AT,
        ),
        policy_record_id="ordinary-policy",
        policy_revision=1,
        policy_sha256="b" * 64,
        credential_id="ordinary-credential",
        credential_version=1,
        credential_digest="c" * 64,
        state=state,  # type: ignore[arg-type]
        reserved_at=0,
        updated_at=0,
    )


def _racing_admission(fixture: _HistoricalCompletionFixture) -> MergeAdmissionRecord:
    readiness = _evaluate_readiness(
        target=_readiness_target(
            repository=REPOSITORY,
            base_branch=BASE_BRANCH,
            pull_request_number=1,
            queue_position=1,
        ),
        candidate_evidence=_readiness_candidate(
            repository=REPOSITORY,
            pull_request_number=1,
            queue_position=1,
        ),
        fence_evidence=_readiness_fence(
            controller_key=fixture.controller.controller_key,
            controller_repository=REPOSITORY,
            controller_base_branch=BASE_BRANCH,
        ),
    )
    expected_effect_sha = readiness.target.expected_effect_sha
    lease_owner = readiness.fence.evidence.observed_lease_owner
    attempt_id = build_merge_effect_attempt_id(
        controller_key=fixture.controller.controller_key,
        lease_owner=lease_owner,
        lease_acquired_at="2026-08-11T03:00:00Z",
        landing_plan_id="landing-plan-1",
        pull_request_number=1,
        queue_position=1,
        attempt_sequence=1,
        expected_effect_sha=expected_effect_sha,
    )
    return _merge_admission(
        readiness=readiness,
        attempt_id=attempt_id,
        repository=REPOSITORY,
        base_branch=BASE_BRANCH,
        pull_request_number=1,
        queue_position=1,
        candidate_sha=expected_effect_sha,
        expected_effect_sha=expected_effect_sha,
        controller_key=fixture.controller.controller_key,
        lease_owner=lease_owner,
    )


def _changed_policy_record(fixture: _HistoricalCompletionFixture) -> MergeTrainPolicyRecord:
    repository_policy = fixture.policy_record.policy.policies[0]
    changed_repository_policy = repository_policy.model_copy(
        update={
            "github_token": repository_policy.github_token.model_copy(
                update={"env_var": "OTHER_GH_TOKEN"}
            )
        }
    )
    changed_policy = fixture.policy_record.policy.model_copy(
        update={"policies": (changed_repository_policy,)}
    )
    return MergeTrainPolicyRecord.model_validate(
        {
            "record_id": "merge-train-policy-changed",
            "status": "active",
            "source": "test:historical-disposition-change",
            "updated_at": "2026-09-13T13:00:00Z",
            "policy": changed_policy.model_dump(mode="json"),
        }
    )


def _fresh_candidate_and_landing(
    fixture: _HistoricalCompletionFixture,
) -> tuple[MergeTrainBatchCandidateRecord, MergeTrainBatchLandingPlanRecord]:
    candidate_payload = fixture.candidate_record.candidate.model_dump(mode="python")
    candidate_payload.update(
        {
            "batch_id": "historical-followup-batch",
            "candidate_ref": build_merge_train_batch_candidate_ref(
                repository=REPOSITORY,
                base_branch=BASE_BRANCH,
                batch_id="historical-followup-batch",
            ),
            "status": "passed",
            "created_at": "2026-09-15T14:00:00Z",
            "updated_at": "2026-09-15T14:00:00Z",
        }
    )
    candidate = MergeTrainBatchCandidate.model_validate(candidate_payload)
    candidate_record = build_merge_train_batch_candidate_record(
        candidate=candidate,
        source="test:historical-disposition-followup",
        updated_at="2026-09-15T14:00:00Z",
    )
    landing_plan = build_merge_train_batch_landing_plan(
        candidate=candidate,
        merge_method="merge",
        created_at="2026-09-15T14:01:00Z",
    )
    landing_record = build_merge_train_batch_landing_plan_record(
        landing_plan=landing_plan,
        source="test:historical-disposition-followup",
        updated_at="2026-09-15T14:01:00Z",
    )
    return candidate_record, landing_record


class NativeHistoricalDispositionPostgresTests(unittest.TestCase):
    def test_atomic_success_persists_successor_retires_source_releases_controller(self) -> None:
        with _prepared_store() as (store, fixture):
            request = _request(fixture)
            authority = store.authorize_merge_train_historical_completion(request)
            snapshot = store.read_merge_train_historical_completion_snapshot(request, authority)
            provider_transport = _ReadOnlyTransport(responses=_provider_responses())
            result = store.finalize_merge_train_historical_completion(
                request=request,
                authority=authority,
                snapshot=snapshot,
                provider_evidence=_provider_evidence(store, fixture, transport=provider_transport),
                trace_id="trace-historical-1",
            )

            controller = store.read_merge_train_controller_state_record(
                fixture.controller.controller_key
            )
            landings = store.list_merge_train_batch_landing_plan_records(
                repository=REPOSITORY, base_branch=BASE_BRANCH
            )
            self.assertEqual(controller.status, "idle")
            self.assertEqual(controller.last_action, "record_historical_completion")
            self.assertEqual(controller.active_record_id, "")
            self.assertEqual(sum(record.status == "superseded" for record in landings), 1)
            successors = [record for record in landings if record.status == "active"]
            self.assertEqual(len(successors), 1)
            successor = successors[0]
            assert successor.historical_completion is not None
            self.assertEqual(
                successor.historical_completion.classification,
                "observed_merged_without_admission",
            )
            self.assertEqual(
                store.list_merge_admission_records(repository=REPOSITORY, base_branch=BASE_BRANCH),
                (),
            )
            stored = store.read_idempotency_record(
                scope=request.scope, route_path=ROUTE, idempotency_key=request.idempotency_key
            )
            assert stored is not None
            self.assertEqual(stored.state, "completed")
            self.assertEqual(result.state, "completed")
            self.assertTrue(provider_transport.requests)
            self.assertTrue(all(request.method == "GET" for request in provider_transport.requests))

    def test_post_disposition_generic_writers_reselect_fresh_landing(self) -> None:
        with _prepared_store() as (store, fixture):
            request = _request(fixture)
            authority = store.authorize_merge_train_historical_completion(request)
            snapshot = store.read_merge_train_historical_completion_snapshot(request, authority)
            store.finalize_merge_train_historical_completion(
                request=request,
                authority=authority,
                snapshot=snapshot,
                provider_evidence=_provider_evidence(store, fixture),
                trace_id="trace-historical-followup",
            )
            historical_successor = next(
                record
                for record in store.list_merge_train_batch_landing_plan_records(
                    repository=REPOSITORY, base_branch=BASE_BRANCH
                )
                if record.historical_completion is not None
            )
            fresh_candidate, fresh_landing = _fresh_candidate_and_landing(fixture)
            store.write_merge_train_batch_candidate_record(fresh_candidate)
            store.write_merge_train_batch_landing_plan_record(fresh_landing)

            decision = decide_merge_train_controller_record_action(
                candidate_records=store.list_merge_train_batch_candidate_records(
                    repository=REPOSITORY, base_branch=BASE_BRANCH
                ),
                landing_plan_records=store.list_merge_train_batch_landing_plan_records(
                    repository=REPOSITORY, base_branch=BASE_BRANCH
                ),
                stack_collapse_plan_records=store.list_merge_train_stack_collapse_plan_records(
                    repository=REPOSITORY, base_branch=BASE_BRANCH
                ),
            )
            self.assertEqual(decision.action, "land_batch")
            self.assertEqual(decision.landing_plan_record_id, fresh_landing.record_id)
            self.assertEqual(
                next(
                    record
                    for record in store.list_merge_train_batch_landing_plan_records(
                        repository=REPOSITORY, base_branch=BASE_BRANCH
                    )
                    if record.record_id == historical_successor.record_id
                ),
                historical_successor,
            )
            self.assertIsNone(fresh_landing.historical_completion)

    def test_legacy_defaulted_source_payload_is_read_and_finalized(self) -> None:
        with _prepared_store() as (store, fixture):
            with store._session_factory() as session:
                row = session.get(
                    LaunchplaneMergeTrainBatchLandingPlanRow,
                    fixture.landing_record.record_id,
                )
                assert row is not None
                row.payload = {
                    key: value for key, value in row.payload.items() if key != "schema_version"
                }
                legacy_payload = dict(row.payload)
                session.commit()

            request = _request(fixture)
            authority = store.authorize_merge_train_historical_completion(request)
            snapshot = store.read_merge_train_historical_completion_snapshot(request, authority)
            self.assertEqual(snapshot.source_payload_sha256, canonical_json_sha256(legacy_payload))
            with store._session_factory() as session:
                row = session.get(
                    LaunchplaneMergeTrainBatchLandingPlanRow,
                    fixture.landing_record.record_id,
                )
                assert row is not None
                self.assertEqual(row.payload, legacy_payload)

            store.finalize_merge_train_historical_completion(
                request=request,
                authority=authority,
                snapshot=snapshot,
                provider_evidence=_provider_evidence(store, fixture),
                trace_id="trace-historical-legacy-default",
            )
            with store._session_factory() as session:
                row = session.get(
                    LaunchplaneMergeTrainBatchLandingPlanRow,
                    fixture.landing_record.record_id,
                )
                assert row is not None
                self.assertNotIn("schema_version", row.payload)

    def test_raw_source_rewrite_after_snapshot_fails_without_history_write(self) -> None:
        with _prepared_store() as (store, fixture):
            request = _request(fixture)
            authority = store.authorize_merge_train_historical_completion(request)
            snapshot = store.read_merge_train_historical_completion_snapshot(request, authority)
            with store._session_factory() as session:
                row = session.get(
                    LaunchplaneMergeTrainBatchLandingPlanRow,
                    fixture.landing_record.record_id,
                )
                assert row is not None
                row.payload = {**row.payload, "ordinary_job_binding": None}
                session.commit()

            with self.assertRaisesRegex(HistoricalDispositionError, "store_state_changed"):
                store.finalize_merge_train_historical_completion(
                    request=request,
                    authority=authority,
                    snapshot=snapshot,
                    provider_evidence=_provider_evidence(store, fixture),
                    trace_id="trace-historical-raw-rewrite",
                )
            self.assertEqual(
                store.read_merge_train_controller_state_record(fixture.controller.controller_key),
                fixture.controller,
            )
            self.assertIsNone(
                store.read_idempotency_record(
                    scope=request.scope,
                    route_path=ROUTE,
                    idempotency_key=request.idempotency_key,
                )
            )

    def test_controller_projection_mismatch_refuses_read_snapshot(self) -> None:
        with _prepared_store() as (store, fixture):
            with store._session_factory() as session:
                row = session.get(
                    LaunchplaneMergeTrainBatchLandingPlanRow,
                    fixture.landing_record.record_id,
                )
                assert row is not None
                row.source = "tampered-source-projection"
                session.commit()

            request = _request(fixture)
            authority = store.authorize_merge_train_historical_completion(request)
            with self.assertRaisesRegex(HistoricalDispositionError, "landing_plan_binding_changed"):
                store.read_merge_train_historical_completion_snapshot(request, authority)
            with store._session_factory() as session:
                row = session.get(
                    LaunchplaneMergeTrainBatchLandingPlanRow,
                    fixture.landing_record.record_id,
                )
                assert row is not None
                self.assertEqual(
                    row.payload, fixture.landing_record.model_dump(mode="json", exclude_none=True)
                )

    def test_same_key_replay_is_currently_authorized_after_controller_work(self) -> None:
        with _prepared_store() as (store, fixture):
            request = _request(fixture)
            authority = store.authorize_merge_train_historical_completion(request)
            snapshot = store.read_merge_train_historical_completion_snapshot(request, authority)
            store.finalize_merge_train_historical_completion(
                request=request,
                authority=authority,
                snapshot=snapshot,
                provider_evidence=_provider_evidence(store, fixture),
                trace_id="trace-historical-replay",
            )
            store.acquire_merge_train_controller_state_record(
                repository=REPOSITORY,
                base_branch=BASE_BRANCH,
                policy_key=fixture.policy_record.policy.policies[0].policy_key,
                policy_sha256=fixture.policy_record.policy_sha256,
                lease_owner="post-disposition-controller",
                lease_seconds=30,
                initial_active_action="select_next_action",
                initial_active_phase="select_next_action",
                adoptable_active_actions=("select_next_action",),
            )
            replay_authority = store.authorize_merge_train_historical_completion(request)
            self.assertIsNotNone(replay_authority.replay_record)
            assert replay_authority.replay_record is not None
            self.assertEqual(replay_authority.replay_record.state, "completed")

    def test_revoked_caller_cannot_replay_completed_receipt(self) -> None:
        with _prepared_store() as (store, fixture):
            request = _request(fixture)
            authority = store.authorize_merge_train_historical_completion(request)
            snapshot = store.read_merge_train_historical_completion_snapshot(request, authority)
            store.finalize_merge_train_historical_completion(
                request=request,
                authority=authority,
                snapshot=snapshot,
                provider_evidence=_provider_evidence(store, fixture),
                trace_id="trace-historical-revoked",
            )
            store.compare_and_write_authz_policy_record(
                expected_record=_authz_policy_record(),
                replacement_record=_authz_policy_record(allow=False, revision=2),
            )
            with self.assertRaisesRegex(HistoricalDispositionError, "authorization_denied"):
                store.authorize_merge_train_historical_completion(request)

    def test_different_key_cannot_reuse_recorded_selector(self) -> None:
        with _prepared_store() as (store, fixture):
            request = _request(fixture, key="history-1")
            authority = store.authorize_merge_train_historical_completion(request)
            snapshot = store.read_merge_train_historical_completion_snapshot(request, authority)
            store.finalize_merge_train_historical_completion(
                request=request,
                authority=authority,
                snapshot=snapshot,
                provider_evidence=_provider_evidence(store, fixture),
                trace_id="trace-historical-different-key",
            )

            different_request = _request(fixture, key="history-2")
            different_authority = store.authorize_merge_train_historical_completion(
                different_request
            )
            with self.assertRaisesRegex(HistoricalDispositionError, "already_recorded"):
                store.read_merge_train_historical_completion_snapshot(
                    different_request, different_authority
                )

    def test_authority_change_between_snapshot_and_finalize_is_atomic(self) -> None:
        for changed_authority in ("authz", "merge_policy"):
            with (
                self.subTest(changed_authority=changed_authority),
                _prepared_store() as (
                    store,
                    fixture,
                ),
            ):
                request = _request(fixture)
                authority = store.authorize_merge_train_historical_completion(request)
                snapshot = store.read_merge_train_historical_completion_snapshot(request, authority)
                evidence = _provider_evidence(store, fixture)
                if changed_authority == "authz":
                    store.compare_and_write_authz_policy_record(
                        expected_record=_authz_policy_record(),
                        replacement_record=_authz_policy_record(allow=False, revision=2),
                    )
                    expected_reason = "authorization_changed"
                else:
                    store.compare_and_write_merge_train_policy_record(
                        expected_record=fixture.policy_record,
                        replacement_record=_changed_policy_record(fixture),
                    )
                    expected_reason = "merge_policy_changed"

                with self.assertRaisesRegex(HistoricalDispositionError, expected_reason):
                    store.finalize_merge_train_historical_completion(
                        request=request,
                        authority=authority,
                        snapshot=snapshot,
                        provider_evidence=evidence,
                        trace_id=f"trace-historical-{changed_authority}-changed",
                    )

                self.assertEqual(
                    store.read_merge_train_controller_state_record(
                        fixture.controller.controller_key
                    ),
                    fixture.controller,
                )
                self.assertEqual(
                    [
                        record.record_id
                        for record in store.list_merge_train_batch_landing_plan_records(
                            repository=REPOSITORY, base_branch=BASE_BRANCH
                        )
                        if record.status == "active"
                    ],
                    [fixture.landing_record.record_id],
                )
                self.assertIsNone(
                    store.read_idempotency_record(
                        scope=request.scope,
                        route_path=ROUTE,
                        idempotency_key=request.idempotency_key,
                    )
                )

    def test_finalize_rolls_back_after_business_write_failure(self) -> None:
        with _prepared_store() as (store, fixture):
            request = _request(fixture)
            authority = store.authorize_merge_train_historical_completion(request)
            snapshot = store.read_merge_train_historical_completion_snapshot(request, authority)

            def fail_after_flush(
                row: LaunchplaneMergeTrainControllerStateRow,
                _record: MergeTrainControllerStateRecord,
            ) -> None:
                session = object_session(row)
                assert session is not None
                session.flush()
                raise RuntimeError("injected-after-business-write")

            with patch.object(
                store,
                "_sync_merge_train_controller_state_row",
                side_effect=fail_after_flush,
            ):
                with self.assertRaisesRegex(RuntimeError, "injected-after-business-write"):
                    store.finalize_merge_train_historical_completion(
                        request=request,
                        authority=authority,
                        snapshot=snapshot,
                        provider_evidence=_provider_evidence(store, fixture),
                        trace_id="trace-historical-rollback",
                    )

            controller = store.read_merge_train_controller_state_record(
                fixture.controller.controller_key
            )
            self.assertEqual(controller, fixture.controller)
            active_landings = store.list_merge_train_batch_landing_plan_records(
                repository=REPOSITORY, base_branch=BASE_BRANCH
            )
            self.assertEqual(
                [record.record_id for record in active_landings if record.status == "active"],
                [fixture.landing_record.record_id],
            )
            self.assertIsNone(
                store.read_idempotency_record(
                    scope=request.scope,
                    route_path=ROUTE,
                    idempotency_key=request.idempotency_key,
                )
            )

    def test_busy_controller_lock_fails_closed_without_business_writes(self) -> None:
        with _prepared_store() as (store, fixture):
            request = _request(fixture)
            authority = store.authorize_merge_train_historical_completion(request)
            snapshot = store.read_merge_train_historical_completion_snapshot(request, authority)
            evidence = _provider_evidence(store, fixture)
            with store._session_factory() as holder:
                holder.execute(
                    text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
                    {"lock_name": fixture.controller.controller_key},
                )
                with ThreadPoolExecutor(max_workers=1) as executor:
                    pending = executor.submit(
                        store.finalize_merge_train_historical_completion,
                        request=request,
                        authority=authority,
                        snapshot=snapshot,
                        provider_evidence=evidence,
                        trace_id="trace-historical-busy",
                    )
                    with self.assertRaisesRegex(HistoricalDispositionError, "recovery_busy"):
                        pending.result(timeout=5)
                holder.rollback()

            self.assertEqual(
                store.read_merge_train_controller_state_record(fixture.controller.controller_key),
                fixture.controller,
            )
            self.assertEqual(
                [
                    record.record_id
                    for record in store.list_merge_train_batch_landing_plan_records(
                        repository=REPOSITORY, base_branch=BASE_BRANCH
                    )
                    if record.status == "active"
                ],
                [fixture.landing_record.record_id],
            )
            self.assertIsNone(
                store.read_idempotency_record(
                    scope=request.scope,
                    route_path=ROUTE,
                    idempotency_key=request.idempotency_key,
                )
            )

    def test_admission_writer_waits_for_controller_locked_commit(self) -> None:
        with _prepared_store() as (store, fixture):
            request = _request(fixture)
            authority = store.authorize_merge_train_historical_completion(request)
            snapshot = store.read_merge_train_historical_completion_snapshot(request, authority)
            evidence = _provider_evidence(store, fixture)
            admission = _racing_admission(fixture)
            entered_business_write = Event()
            release_business_write = Event()
            original_sync = store._sync_merge_train_controller_state_row

            def hold_before_controller_sync(
                row: LaunchplaneMergeTrainControllerStateRow,
                record: MergeTrainControllerStateRecord,
            ) -> None:
                session = object_session(row)
                assert session is not None
                session.flush()
                entered_business_write.set()
                self.assertTrue(release_business_write.wait(timeout=5))
                original_sync(row, record)

            def write_admission_after_controller_lock() -> tuple[MergeAdmissionRecord, bool]:
                return store.create_merge_admission_record_if_absent(admission)

            with patch.object(
                store,
                "_sync_merge_train_controller_state_row",
                side_effect=hold_before_controller_sync,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    finalize_pending = executor.submit(
                        store.finalize_merge_train_historical_completion,
                        request=request,
                        authority=authority,
                        snapshot=snapshot,
                        provider_evidence=evidence,
                        trace_id="trace-historical-admission-race",
                    )
                    self.assertTrue(entered_business_write.wait(timeout=5))
                    writer_pending = executor.submit(write_admission_after_controller_lock)
                    with self.assertRaises(TimeoutError):
                        writer_pending.result(timeout=0.2)
                    release_business_write.set()
                    finalize_pending.result(timeout=5)
                    written_admission, created = writer_pending.result(timeout=5)
                    self.assertTrue(created)
                    self.assertEqual(written_admission, admission)

            self.assertEqual(
                store.read_merge_train_controller_state_record(
                    fixture.controller.controller_key
                ).status,
                "idle",
            )
            self.assertEqual(
                store.read_merge_admission_record(admission.admission_id),
                admission,
            )

    def test_conservative_absence_gates_block_other_head_and_lineage_admission(self) -> None:
        with _prepared_store() as (store, fixture):
            request = _request(fixture)
            with store._session_factory() as session:
                session.add(
                    LaunchplaneMergeAdmissionRow(
                        admission_id="other-head-admission",
                        admission_binding_sha256="x",
                        attempt_id="other-head-attempt",
                        attempt_sequence=1,
                        decision="admitted",
                        repository=REPOSITORY,
                        base_branch=BASE_BRANCH,
                        pull_request_number=1,
                        queue_position=1,
                        landing_plan_record_id="other-lineage",
                        landing_plan_id="other-lineage",
                        created_at=OBSERVED_AT,
                        payload={},
                    )
                )
                session.commit()
                with self.assertRaisesRegex(HistoricalDispositionError, "admission"):
                    _all_target_admissions(session, request, (1, 2))

    def test_active_stack_member_and_malformed_progress_fail_closed(self) -> None:
        with _prepared_store() as (store, fixture):
            store.write_merge_train_stack_collapse_plan_record(_stack_member_record(fixture))
            request = _request(fixture)
            authority = store.authorize_merge_train_historical_completion(request)
            with self.assertRaisesRegex(HistoricalDispositionError, "stack"):
                store.read_merge_train_historical_completion_snapshot(request, authority)
        with _prepared_store() as (malformed_store, malformed_fixture):
            seed_crowded_scoped_history(malformed_store, malformed_fixture, stack_count=1)
            with malformed_store._session_factory() as session:
                row = session.scalar(
                    select(LaunchplaneMergeTrainStackCollapsePlanRow).where(
                        LaunchplaneMergeTrainStackCollapsePlanRow.status == "active"
                    )
                )
                assert row is not None
                row.payload = {"plan": {"entries": "malformed"}}
                session.commit()
            malformed_request = _request(malformed_fixture)
            malformed_authority = malformed_store.authorize_merge_train_historical_completion(
                malformed_request
            )
            with self.assertRaises(HistoricalDispositionError):
                malformed_store.read_merge_train_historical_completion_snapshot(
                    malformed_request, malformed_authority
                )

    def test_any_ordinary_effect_state_for_target_blocks(self) -> None:
        for state in ("completed", "unknown_future_state"):
            with self.subTest(state=state), _prepared_store() as (store, fixture):
                with store._session_factory() as session:
                    effect = _ordinary_effect_record(
                        fixture, state="completed" if state == "unknown_future_state" else state
                    )
                    payload = effect.model_dump(mode="json")
                    if state == "unknown_future_state":
                        payload["state"] = state
                    session.add(
                        LaunchplaneOrdinaryAgentEffectRow(
                            effect_id=effect.effect_id,
                            lease_id=effect.lease_id,
                            request_id=effect.request_id,
                            scope_sha256=effect.scope_sha256,
                            binding_revision=effect.binding_revision,
                            action_ordinal=effect.action_ordinal,
                            semantic_key="1:stack_child_label",
                            command_sha256=effect.command_sha256,
                            revision=effect.revision,
                            payload=payload,
                        )
                    )
                    session.commit()
                request = _request(fixture)
                authority = store.authorize_merge_train_historical_completion(request)
                with self.assertRaisesRegex(HistoricalDispositionError, "ordinary"):
                    store.read_merge_train_historical_completion_snapshot(request, authority)

    def test_malformed_active_progress_fails_closed(self) -> None:
        with _prepared_store() as (store, fixture):
            with store._session_factory() as session:
                row = session.get(
                    LaunchplaneMergeTrainBatchCandidateRow,
                    fixture.candidate_record.record_id,
                )
                assert row is not None
                row.payload = {"candidate": {"repository": REPOSITORY}}
                session.commit()
            request = _request(fixture)
            authority = store.authorize_merge_train_historical_completion(request)
            with self.assertRaises(HistoricalDispositionError):
                store.read_merge_train_historical_completion_snapshot(request, authority)

    def test_source_change_after_snapshot_fails_without_successor(self) -> None:
        with _prepared_store() as (store, fixture):
            request = _request(fixture)
            authority = store.authorize_merge_train_historical_completion(request)
            snapshot = store.read_merge_train_historical_completion_snapshot(request, authority)
            evidence = _provider_evidence(store, fixture)
            with store._session_factory() as session:
                landing = session.execute(
                    select(LaunchplaneMergeTrainBatchLandingPlanRow).where(
                        LaunchplaneMergeTrainBatchLandingPlanRow.record_id
                        == fixture.landing_record.record_id
                    )
                ).scalar_one()
                landing.payload = {
                    **landing.payload,
                    "landing_plan": {
                        **landing.payload["landing_plan"],
                        "updated_at": "2026-09-13T13:00:00Z",
                    },
                }
                session.commit()
            with self.assertRaises(HistoricalDispositionError):
                store.finalize_merge_train_historical_completion(
                    request=request,
                    authority=authority,
                    snapshot=snapshot,
                    provider_evidence=evidence,
                    trace_id="trace-source-changed",
                )
            with store._session_factory() as session:
                self.assertEqual(
                    session.scalar(
                        select(func.count())
                        .select_from(LaunchplaneMergeTrainBatchLandingPlanRow)
                        .where(LaunchplaneMergeTrainBatchLandingPlanRow.status == "active")
                    ),
                    1,
                )

    def test_sqlite_backend_is_rejected_without_writes(self) -> None:
        from control_plane.storage.postgres import PostgresRecordStore

        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{Path(directory) / 'db.sqlite'}"
            )
            try:
                store.ensure_schema()
                fixture = _HistoricalCompletionFixture(Path(directory) / "seed")
                _seed_fixture(store, fixture)
                request = _request(fixture)
                with self.assertRaises(Exception):
                    store.authorize_merge_train_historical_completion(request)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
