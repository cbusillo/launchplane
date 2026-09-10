from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Never
import unittest
from unittest.mock import patch

from sqlalchemy import select

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchEntry,
    build_merge_train_batch_candidate_record,
    build_ordinary_merge_train_candidate_ref,
)
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeEffect,
    CandidateHeadMergeOutcome,
    CandidateRefDeleteEffect,
    CandidateRefPrepareEffect,
    MergeTrainEffectLineage,
    PullRequestHeadRefreshEffect,
    PullRequestLandingEffect,
    StackChildCloseEffect,
)
from control_plane.contracts.ordinary_agent import OrdinaryAgentPullRequest, OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentJobBinding,
)
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.ordinary_agent_effect_router import (
    OrdinaryAgentEffectRouteDeferred,
    OrdinaryAgentSemanticEffectRouter,
)
from control_plane.ordinary_agent_merge_train_executor import OrdinaryAgentEffectTerminal
from control_plane.ordinary_agent_session_lifecycle import (
    OrdinaryAgentSessionAdmissionDenied,
)
from control_plane.storage.postgres import LaunchplaneOrdinaryAgentEffectRow
from tests import test_ordinary_agent_effect_storage as effect_support
from tests import test_ordinary_agent_landing_storage as landing_support


class _HistoryStore:
    def __init__(
        self,
        history: effects.OrdinaryAgentEffectHistory,
        *,
        reserved: effects.OrdinaryAgentEffectRecord | None = None,
    ) -> None:
        self.history = history
        self.reserved = reserved if reserved is not None else history.effect
        self.reservations: list[dict[str, object]] = []

    def reserve_ordinary_agent_effect(self, **kwargs: object) -> effects.OrdinaryAgentEffectRecord:
        self.reservations.append(kwargs)
        return self.reserved

    def read_ordinary_agent_effect_history(
        self, *, effect_id: str
    ) -> effects.OrdinaryAgentEffectHistory:
        if effect_id != self.history.effect.effect_id:
            raise AssertionError("unexpected effect")
        return self.history


def _request() -> OrdinaryAgentFiniteRequestRecord:
    return OrdinaryAgentFiniteRequestRecord(
        request_id="request-one",
        idempotency_key="request-one",
        principal_id="agent-one",
        session_id="session-one",
        lease_id="lease-one",
        target=OrdinaryAgentTarget(
            repository_id=123,
            repository="example/repo",
            base_branch="main",
        ),
        base_sha="a" * 40,
        pull_requests=(
            OrdinaryAgentPullRequest(number=12, head_sha="b" * 40),
            OrdinaryAgentPullRequest(number=13, head_sha="c" * 40),
        ),
        permitted_stack_edit_pull_requests=(),
        refresh_allowance_total=1,
        admitted_at=1,
        expires_at=100,
    )


def _record(
    request: OrdinaryAgentFiniteRequestRecord,
    command: effects.OrdinaryAgentSemanticCommand,
    *,
    state: effects.EffectState,
    semantic_ordinal: int,
    next_observation_at: int | None = None,
) -> effects.OrdinaryAgentEffectRecord:
    return effects.OrdinaryAgentEffectRecord(
        effect_id="effect-one",
        request_id=request.request_id,
        session_id=request.session_id,
        lease_id=request.lease_id,
        principal_id=request.principal_id,
        scope_sha256=request.scope_sha256,
        binding_revision=request.binding_revision,
        semantic_ordinal=semantic_ordinal,
        action_ordinal=1,
        command_sha256=canonical_json_sha256(command.model_dump(mode="json")),
        command=command,
        target=request.target,
        controller_fence=effects.OrdinaryAgentControllerFence(
            controller_key="example/repo:main",
            lease_owner="ordinary-worker",
            lease_acquired_at="2026-09-09T00:00:00Z",
        ),
        policy_record_id="policy-one",
        policy_revision=1,
        policy_sha256="d" * 64,
        credential_id="credential-one",
        credential_version=1,
        credential_digest="e" * 64,
        state=state,
        dispatch_count=1 if state != "reserved" else 0,
        next_observation_at=next_observation_at,
        reserved_at=1,
        updated_at=1,
    )


def _child(
    record: effects.OrdinaryAgentEffectRecord,
) -> effects.OrdinaryAgentSemanticDispatchAttemptRecord:
    return effects.OrdinaryAgentSemanticDispatchAttemptRecord(
        child_id="dispatch-one",
        effect_id=record.effect_id,
        semantic_ordinal=record.dispatch_count,  # Child ordinal counts dispatch attempts.
        custody_attempt_id="custody-one",
        dispatch_checkpoint_at=2,
        fixed_token_expires_at=90,
        controller_fence=record.controller_fence,
        command_sha256=record.command_sha256,
    )


def _router(
    request: OrdinaryAgentFiniteRequestRecord, store: object
) -> OrdinaryAgentSemanticEffectRouter:
    return OrdinaryAgentSemanticEffectRouter(
        request=request,
        controller_fence=lambda: effects.OrdinaryAgentControllerFence(
            controller_key="example/repo:main",
            lease_owner="current-worker",
            lease_acquired_at="2026-09-09T00:01:00Z",
        ),
        store=store,  # type: ignore[arg-type]
    )


class OrdinaryAgentEffectRouterTests(unittest.TestCase):
    def test_candidate_replay_decodes_normal_and_noop_without_dispatch(self) -> None:
        request = _request()
        for no_op in (False, True):
            with self.subTest(no_op=no_op):
                effect = CandidateHeadMergeEffect(
                    lineage=MergeTrainEffectLineage(
                        repository=request.target.repository,
                        base_branch=request.target.base_branch,
                        batch_id="batch-one",
                    ),
                    candidate_ref="refs/heads/candidate-one",
                    rolling_parent_sha=request.base_sha,
                    pull_request_number=13,
                    head_sha=request.pull_requests[1].head_sha,
                )
                command = effects.CandidateHeadMergeCommand(effect=effect)
                record = _record(request, command, state="completed", semantic_ordinal=2)
                result_sha = effect.rolling_parent_sha if no_op else "f" * 40
                proof = effects.OrdinaryAgentRefObservation(
                    repository=request.target.repository,
                    ref=effect.candidate_ref,
                    sha=result_sha,
                    tree_sha="1" * 40,
                    parents=("9" * 40,) if no_op else (effect.rolling_parent_sha, effect.head_sha),
                    contained_head_sha=effect.head_sha if no_op else None,
                )
                history = effects.OrdinaryAgentEffectHistory(
                    effect=record,
                    child=_child(record),
                    outcome=effects.OrdinaryAgentCompletedOutcome(
                        result_sha=result_sha,
                        no_op=no_op,
                        proof=proof,
                    ),
                )
                # A concurrent observation may complete the reserved effect
                # before the consistent history read. Replay that proof, not a
                # stale reservation or an artificial binding-denied result.
                history = history.model_copy(
                    update={"effect": record.model_copy(update={"revision": record.revision + 1})}
                )
                store = _HistoryStore(
                    history,
                    reserved=record.model_copy(update={"state": "reserved", "dispatch_count": 0}),
                )
                with patch(
                    "control_plane.ordinary_agent_effect_router.OrdinaryAgentMergeTrainEffectExecutor"
                ) as executor:
                    result = _router(request, store).merge_candidate_head(effect)
                executor.assert_not_called()
                self.assertEqual(store.reservations[0]["semantic_ordinal"], 2)
                self.assertEqual(
                    result,
                    CandidateHeadMergeOutcome(
                        result_sha=None if no_op else result_sha,
                        result_tree_sha=proof.tree_sha,
                        parent_shas=() if no_op else proof.parents,
                    ),
                )

    def test_observe_rebind_terminal_and_binding_conflict_stay_typed(self) -> None:
        request = _request()
        merge_effect = CandidateHeadMergeEffect(
            lineage=MergeTrainEffectLineage(
                repository=request.target.repository,
                base_branch=request.target.base_branch,
                batch_id="batch-one",
            ),
            candidate_ref="refs/heads/candidate-one",
            rolling_parent_sha=request.base_sha,
            pull_request_number=12,
            head_sha=request.pull_requests[0].head_sha,
        )
        merge_command = effects.CandidateHeadMergeCommand(effect=merge_effect)
        observe = _record(
            request,
            merge_command,
            state="reconciliation_required",
            semantic_ordinal=1,
            next_observation_at=45,
        )
        observe_history = effects.OrdinaryAgentEffectHistory(
            effect=observe,
            child=_child(observe),
            outcome=effects.OrdinaryAgentUnknownOutcome(reason="response_ambiguous"),
        )
        with self.assertRaises(OrdinaryAgentEffectRouteDeferred) as caught:
            _router(request, _HistoryStore(observe_history)).merge_candidate_head(merge_effect)
        self.assertEqual(
            (
                caught.exception.disposition,
                caught.exception.reason_code,
                caught.exception.next_observation_at,
            ),
            ("observe", "provider_outcome_unknown", 45),
        )

        refresh_effect = PullRequestHeadRefreshEffect(
            lineage=merge_effect.lineage,
            pull_request_number=12,
            expected_head_sha=request.pull_requests[0].head_sha,
            expected_base_sha=request.base_sha,
        )
        refresh_command = effects.PullRequestHeadRefreshCommand(effect=refresh_effect)
        rebound = _record(request, refresh_command, state="rebind_pending", semantic_ordinal=1)
        refresh_proof = effects.OrdinaryAgentPullRequestObservation(
            repository=request.target.repository,
            number=12,
            head_sha="2" * 40,
            base_ref=request.target.base_branch,
            base_sha=request.base_sha,
            state="open",
            merged=False,
            head_parents=(request.pull_requests[0].head_sha, request.base_sha),
        )
        rebound_history = effects.OrdinaryAgentEffectHistory(
            effect=rebound,
            child=_child(rebound),
            outcome=effects.OrdinaryAgentCompletedOutcome(
                result_sha=refresh_proof.head_sha,
                proof=refresh_proof,
            ),
        )
        with self.assertRaises(OrdinaryAgentEffectRouteDeferred) as caught:
            _router(request, _HistoryStore(rebound_history)).refresh_pull_request_head(
                refresh_effect
            )
        self.assertEqual(caught.exception.disposition, "rebind")

        terminal = observe.model_copy(update={"state": "not_dispatched"})
        terminal_history = effects.OrdinaryAgentEffectHistory(
            effect=terminal,
            child=_child(terminal),
            outcome=effects.OrdinaryAgentKnownNotDispatchedOutcome(reason="provider_rejected"),
        )
        with self.assertRaisesRegex(OrdinaryAgentEffectTerminal, "provider_rejected"):
            _router(request, _HistoryStore(terminal_history)).merge_candidate_head(merge_effect)

        mismatched = observe.model_copy(update={"request_id": "different-request"})
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "effect_history_binding_conflict"
        ):
            _router(
                request,
                _HistoryStore(observe_history.model_copy(update={"effect": mismatched})),
            ).merge_candidate_head(merge_effect)

        # A stale history read must not turn a newer reservation into dispatch.
        with (
            self.assertRaisesRegex(
                OrdinaryAgentSessionAdmissionDenied, "effect_history_binding_conflict"
            ),
            patch(
                "control_plane.ordinary_agent_effect_router.OrdinaryAgentMergeTrainEffectExecutor"
            ) as executor,
        ):
            _router(
                request,
                _HistoryStore(
                    observe_history,
                    reserved=observe.model_copy(update={"revision": observe.revision + 1}),
                ),
            ).merge_candidate_head(merge_effect)
        executor.assert_not_called()

    def test_unknown_pr_and_unsupported_methods_reserve_nothing(self) -> None:
        request = _request()
        placeholder = effects.CandidateRefPrepareCommand(
            effect=CandidateRefPrepareEffect(
                lineage=MergeTrainEffectLineage(
                    repository=request.target.repository,
                    base_branch=request.target.base_branch,
                    batch_id="batch-one",
                ),
                candidate_ref="refs/heads/candidate-one",
                base_sha=request.base_sha,
            )
        )
        store = _HistoryStore(
            effects.OrdinaryAgentEffectHistory(
                effect=_record(request, placeholder, state="reserved", semantic_ordinal=1)
            )
        )
        router = _router(request, store)
        unknown = CandidateHeadMergeEffect(
            lineage=placeholder.effect.lineage,
            candidate_ref=placeholder.effect.candidate_ref,
            rolling_parent_sha=request.base_sha,
            pull_request_number=99,
            head_sha="f" * 40,
        )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "effect_scope_conflict"):
            router.merge_candidate_head(unknown)
        with self.assertRaises(OrdinaryAgentEffectTerminal):
            router.land_pull_request(
                PullRequestLandingEffect(
                    lineage=placeholder.effect.lineage,
                    pull_request_number=12,
                    head_sha=request.pull_requests[0].head_sha,
                    rolling_base_sha=request.base_sha,
                    admission_id="admission-one",
                    merge_method="merge",
                )
            )
        with self.assertRaises(OrdinaryAgentEffectTerminal):
            router.close_stack_child(
                StackChildCloseEffect(
                    lineage=placeholder.effect.lineage,
                    pull_request_number=12,
                    expected_head_sha=request.pull_requests[0].head_sha,
                )
            )
        self.assertEqual(store.reservations, [])


class OrdinaryAgentEffectRouterStorageTests(unittest.TestCase):
    def test_actual_store_dispatches_prepare_once_then_replays_without_provider_work(self) -> None:
        fixture = effect_support.OrdinaryAgentEffectStorageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fence, _ = fixture.prepare_controller()
        request = fixture.request
        binding = OrdinaryAgentJobBinding(
            request_id=request.request_id,
            scope_sha256=request.scope_sha256,
            binding_revision=request.binding_revision,
        )
        candidate = MergeTrainBatchCandidate(
            batch_id="router-batch",
            repository=request.target.repository,
            base_branch=request.target.base_branch,
            base_sha=request.base_sha,
            policy_key=fixture.merge_policy.policy.policies[0].policy_key,
            policy_sha256=fixture.merge_policy.policy_sha256,
            candidate_ref=build_ordinary_merge_train_candidate_ref(
                binding=binding, batch_id="router-batch"
            ),
            entries=tuple(
                MergeTrainBatchEntry(
                    pull_request_number=item.number,
                    position=position,
                    head_sha=item.head_sha,
                )
                for position, item in enumerate(request.pull_requests, start=1)
            ),
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        wrapper = build_merge_train_batch_candidate_record(
            candidate=candidate,
            source="test:ordinary-router",
            updated_at=candidate.updated_at,
            ordinary_job_binding=binding,
        )
        fixture.store.write_ordinary_merge_train_record(
            request_id=request.request_id,
            expected_binding_revision=request.binding_revision,
            controller_fence=fence,
            record=wrapper,
        )
        transport = RecordingMergeTrainGitHubTransport(
            responses=({"ref": candidate.candidate_ref, "object": {"sha": request.base_sha}},)
        )
        mint_count = 0

        def mint(**kwargs: object) -> GitHubAppInstallationToken:
            nonlocal mint_count
            mint_count += 1
            callback = kwargs["before_token_mint"]
            app_id = getattr(kwargs["identity"], "app_id", None)
            assert callable(callback)
            assert isinstance(app_id, int)
            callback(app_id, 77)
            return GitHubAppInstallationToken(
                token="test-token",
                app_id=app_id,
                installation_id=77,
                repository_id=request.target.repository_id,
                repository=request.target.repository,
                expires_at=datetime.fromtimestamp(
                    fixture.fixture.now + 300, timezone.utc
                ).isoformat(),
            )

        router = OrdinaryAgentSemanticEffectRouter(
            request=request,
            controller_fence=lambda: fence,
            store=fixture.store,
            api_request=lambda **kwargs: None,
            transport_factory=lambda token: transport,
            monotonic=lambda: 0,
            utc_now=lambda: datetime.fromtimestamp(fixture.fixture.now, timezone.utc),
        )
        effect = CandidateRefPrepareEffect(
            lineage=MergeTrainEffectLineage(
                repository=request.target.repository,
                base_branch=request.target.base_branch,
                batch_id=candidate.batch_id,
            ),
            candidate_ref=candidate.candidate_ref,
            base_sha=request.base_sha,
        )
        with (
            patch(
                "control_plane.ordinary_agent_custody.resolve_ordinary_agent_github_app_identity",
                side_effect=lambda **kwargs: SimpleNamespace(
                    identity=SimpleNamespace(app_id=kwargs["candidate"].expected_app_id)
                ),
            ),
            patch(
                "control_plane.ordinary_agent_custody.mint_ordinary_agent_installation_token",
                side_effect=mint,
            ),
        ):
            router.prepare_candidate_ref(effect)
            with self.assertRaisesRegex(
                OrdinaryAgentSessionAdmissionDenied, "effect_replay_conflict"
            ):
                router.prepare_candidate_ref(
                    CandidateRefPrepareEffect(
                        lineage=effect.lineage,
                        candidate_ref=effect.candidate_ref + "-different",
                        base_sha=effect.base_sha,
                    )
                )
            router.prepare_candidate_ref(effect)
        self.assertEqual(mint_count, 1)
        self.assertEqual([item.method for item in transport.requests], ["POST"])

    def test_actual_store_retained_cleanup_replays_without_second_action(self) -> None:
        fixture = landing_support.OrdinaryAgentLandingStorageTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        plan = fixture.plan.landing_plan

        def no_provider_work(*args: object, **kwargs: object) -> Never:
            raise AssertionError("retained cleanup must not reach the provider")

        router = OrdinaryAgentSemanticEffectRouter(
            request=fixture.request,
            controller_fence=lambda: fixture.fence,
            store=fixture.store,
            api_request=no_provider_work,
            transport_factory=no_provider_work,
        )
        effect = CandidateRefDeleteEffect(
            lineage=MergeTrainEffectLineage(
                repository=plan.repository,
                base_branch=plan.base_branch,
                batch_id=plan.batch_id,
                landing_plan_id=plan.plan_id,
            ),
            candidate_ref=plan.candidate_ref,
            expected_ref_sha=plan.candidate_sha,
        )
        self.assertFalse(router.delete_candidate_ref(effect))
        self.assertFalse(router.delete_candidate_ref(effect))
        with fixture.store._session_factory() as session:
            stored = tuple(
                session.scalars(
                    select(LaunchplaneOrdinaryAgentEffectRow).where(
                        LaunchplaneOrdinaryAgentEffectRow.request_id == fixture.request.request_id
                    )
                )
            )
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].payload["state"], "retained_no_conditional_delete")
