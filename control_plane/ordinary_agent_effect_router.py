"""History-aware semantic effects for one finite ordinary-agent binding."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import time
from typing import Literal, Never, Protocol, TypeVar, cast

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeEffect,
    CandidateHeadMergeOutcome,
    CandidateRefDeleteEffect,
    CandidateRefPrepareEffect,
    PullRequestHeadRefreshEffect,
    PullRequestLandingEffect,
    StackChildCloseEffect,
    StackChildCommentEffect,
    StackChildLabelEffect,
    StackChildMergeEffect,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
)
from control_plane.github_app_identity import GitHubApiRequest
from control_plane.merge_train_github import MergeTrainGitHubTransport
from control_plane.ordinary_agent_custody import (
    OrdinaryAgentCustodyAttemptStore,
    OrdinaryAgentCustodySecretStore,
)
from control_plane.ordinary_agent_effect_recovery import recover_ordinary_effect
from control_plane.ordinary_agent_merge_train_executor import (
    OrdinaryAgentEffectTerminal,
    OrdinaryAgentMergeTrainEffectExecutor,
)
from control_plane.ordinary_agent_session_lifecycle import (
    OrdinaryAgentSessionAdmissionDenied,
)
from control_plane.workflows.launchplane import github_api_request


class OrdinaryAgentSemanticRouterStore(
    effects.OrdinaryAgentEffectStore,
    OrdinaryAgentCustodyAttemptStore,
    OrdinaryAgentCustodySecretStore,
    Protocol,
):
    pass


class OrdinaryAgentEffectRouteDeferred(RuntimeError):
    """A job-level recovery step is required before this command can resume."""

    def __init__(
        self,
        *,
        effect_id: str,
        disposition: Literal["observe", "rebind"],
        reason_code: str,
        next_observation_at: int | None,
    ) -> None:
        self.effect_id = effect_id
        self.disposition = disposition
        self.reason_code = reason_code
        self.next_observation_at = next_observation_at
        super().__init__(reason_code)


_Result = TypeVar("_Result")


class OrdinaryAgentSemanticEffectRouter:
    """Reserve, recover, or dispatch one exact ordinary semantic command."""

    def __init__(
        self,
        *,
        request: OrdinaryAgentFiniteRequestRecord,
        controller_fence: Callable[[], effects.OrdinaryAgentControllerFence],
        store: OrdinaryAgentSemanticRouterStore,
        api_request: GitHubApiRequest = github_api_request,
        transport_factory: Callable[[str], MergeTrainGitHubTransport] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._request = request
        self._controller_fence = controller_fence
        self._store = store
        self._api_request = api_request
        self._transport_factory = transport_factory
        self._monotonic = monotonic
        self._utc_now = utc_now

    def prepare_candidate_ref(self, effect: CandidateRefPrepareEffect) -> None:
        command = effects.CandidateRefPrepareCommand(effect=effect)
        route, value = self._route(
            command=command,
            semantic_ordinal=1,
            dispatch=lambda executor: executor.prepare_candidate_ref(effect),
        )
        if route == "replay":
            self._require_completed(value)

    def merge_candidate_head(self, effect: CandidateHeadMergeEffect) -> CandidateHeadMergeOutcome:
        command = effects.CandidateHeadMergeCommand(effect=effect)
        route, value = self._route(
            command=command,
            semantic_ordinal=self._pull_request_ordinal(effect.pull_request_number),
            dispatch=lambda executor: executor.merge_candidate_head(effect),
        )
        if route == "dispatch":
            return cast(CandidateHeadMergeOutcome, value)
        completed = self._require_completed(value)
        proof = completed.proof
        if not isinstance(proof, effects.OrdinaryAgentRefObservation) or not proof.tree_sha:
            raise OrdinaryAgentSessionAdmissionDenied("effect_proof_conflict")
        if completed.no_op:
            return CandidateHeadMergeOutcome(
                result_sha=None,
                result_tree_sha=proof.tree_sha,
            )
        if not completed.result_sha:
            raise OrdinaryAgentSessionAdmissionDenied("effect_proof_conflict")
        return CandidateHeadMergeOutcome(
            result_sha=completed.result_sha,
            result_tree_sha=proof.tree_sha,
            parent_shas=proof.parents,
        )

    def refresh_pull_request_head(self, effect: PullRequestHeadRefreshEffect) -> None:
        command = effects.PullRequestHeadRefreshCommand(effect=effect)
        route, value = self._route(
            command=command,
            semantic_ordinal=self._pull_request_ordinal(effect.pull_request_number),
            dispatch=lambda executor: executor.refresh_pull_request_head(effect),
        )
        if route == "replay":
            self._require_completed(value)

    def delete_candidate_ref(self, effect: CandidateRefDeleteEffect) -> bool:
        command = effects.CandidateRefDeleteCommand(effect=effect)
        route, value = self._route(
            command=command,
            semantic_ordinal=1,
            dispatch=lambda executor: executor.delete_candidate_ref(effect),
        )
        if route == "retained":
            return False
        if route == "dispatch":
            return cast(bool, value)
        raise OrdinaryAgentSessionAdmissionDenied("effect_history_conflict")

    def merge_stack_child(self, effect: StackChildMergeEffect) -> str:
        del effect
        self._unsupported()

    def land_pull_request(self, effect: PullRequestLandingEffect) -> str:
        del effect
        self._unsupported()

    def comment_stack_child(self, effect: StackChildCommentEffect) -> str:
        del effect
        self._unsupported()

    def label_stack_child(self, effect: StackChildLabelEffect) -> None:
        del effect
        self._unsupported()

    def close_stack_child(self, effect: StackChildCloseEffect) -> None:
        del effect
        self._unsupported()

    def _route(
        self,
        *,
        command: effects.OrdinaryAgentSemanticCommand,
        semantic_ordinal: int,
        dispatch: Callable[[OrdinaryAgentMergeTrainEffectExecutor], _Result],
    ) -> tuple[
        Literal["dispatch", "replay", "retained"],
        _Result | effects.OrdinaryAgentCompletedOutcome | None,
    ]:
        fence = self._controller_fence()
        reserved = self._store.reserve_ordinary_agent_effect(
            request_id=self._request.request_id,
            expected_binding_revision=self._request.binding_revision,
            controller_fence=fence,
            command=command,
            semantic_ordinal=semantic_ordinal,
        )
        history = self._store.read_ordinary_agent_effect_history(effect_id=reserved.effect_id)
        record = history.effect
        self._require_history_binding(
            reserved=reserved,
            record=record,
            command=command,
            controller_key=fence.controller_key,
            semantic_ordinal=semantic_ordinal,
        )
        recovery = recover_ordinary_effect(history)
        if recovery.disposition in {"fresh", "retry"}:
            executor = OrdinaryAgentMergeTrainEffectExecutor(
                record=record,
                controller_fence=fence,
                effect_store=self._store,
                custody_store=self._store,
                secret_store=self._store,
                api_request=self._api_request,
                transport_factory=self._transport_factory,
                monotonic=self._monotonic,
                utc_now=self._utc_now,
            )
            return "dispatch", dispatch(executor)
        if recovery.disposition == "replay":
            return "replay", recovery.completed
        if recovery.disposition == "retained":
            if command.kind != "candidate_ref_delete":
                raise OrdinaryAgentSessionAdmissionDenied("effect_history_conflict")
            return "retained", None
        if recovery.disposition in {"observe", "rebind"}:
            raise OrdinaryAgentEffectRouteDeferred(
                effect_id=record.effect_id,
                disposition=recovery.disposition,
                reason_code=recovery.reason_code or f"effect_{recovery.disposition}_required",
                next_observation_at=record.next_observation_at,
            )
        raise OrdinaryAgentEffectTerminal(
            recovery.reason_code or "effect_history_evidence_unavailable"
        )

    def _pull_request_ordinal(self, pull_request_number: int) -> int:
        for ordinal, pull_request in enumerate(self._request.pull_requests, start=1):
            if pull_request.number == pull_request_number:
                return ordinal
        raise OrdinaryAgentSessionAdmissionDenied("effect_scope_conflict")

    def _require_history_binding(
        self,
        *,
        reserved: effects.OrdinaryAgentEffectRecord,
        record: effects.OrdinaryAgentEffectRecord,
        command: effects.OrdinaryAgentSemanticCommand,
        controller_key: str,
        semantic_ordinal: int,
    ) -> None:
        # Reconciliation may advance history after reservation. Use its latest
        # validated proof; a dispatch still joins the current fence and revision.
        if (
            record.effect_id != reserved.effect_id
            or record.revision < reserved.revision
            or record.request_id != self._request.request_id
            or record.binding_revision != self._request.binding_revision
            or record.scope_sha256 != self._request.scope_sha256
            or record.target != self._request.target
            or record.semantic_ordinal != semantic_ordinal
            or record.controller_fence.controller_key != controller_key
            or record.command != command
            or record.command_sha256 != canonical_json_sha256(command.model_dump(mode="json"))
        ):
            raise OrdinaryAgentSessionAdmissionDenied("effect_history_binding_conflict")

    @staticmethod
    def _require_completed(value: object) -> effects.OrdinaryAgentCompletedOutcome:
        if not isinstance(value, effects.OrdinaryAgentCompletedOutcome):
            raise OrdinaryAgentSessionAdmissionDenied("effect_proof_conflict")
        return value

    @staticmethod
    def _unsupported() -> Never:
        raise OrdinaryAgentEffectTerminal("ordinary_effect_method_unsupported")
