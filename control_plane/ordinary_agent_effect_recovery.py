"""Interpret durable effect history without granting or repeating provider work."""

from dataclasses import dataclass
from typing import Literal

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.ordinary_agent_effect_lifecycle import (
    classify_effect_reconciliation,
    require_completed_effect_proof,
)
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied


@dataclass(frozen=True)
class OrdinaryEffectRecovery:
    disposition: Literal["fresh", "retry", "replay", "retained", "rebind", "observe", "terminal"]
    completed: effects.OrdinaryAgentCompletedOutcome | None = None
    reason_code: str | None = None


def recover_ordinary_effect(history: effects.OrdinaryAgentEffectHistory) -> OrdinaryEffectRecovery:
    """A fresh result still requires the joined reservation and custody fences.

    Only the latest dispatch child is supplied by the consistent history reader.
    A checkpoint without a response is uncertain, never permission to resend.
    """
    try:
        return _recover_ordinary_effect(history)
    except OrdinaryAgentSessionAdmissionDenied as error:
        if error.reason_code == "effect_history_binding_conflict":
            raise
        return OrdinaryEffectRecovery("terminal", reason_code=error.reason_code)


def _recover_ordinary_effect(history: effects.OrdinaryAgentEffectHistory) -> OrdinaryEffectRecovery:
    record = history.effect
    child = history.child
    if child is not None and (
        child.effect_id != record.effect_id or child.command_sha256 != record.command_sha256
    ):
        raise OrdinaryAgentSessionAdmissionDenied("effect_history_binding_conflict")
    if child is None and (history.outcome is not None or history.reconciliations):
        raise OrdinaryAgentSessionAdmissionDenied("effect_history_binding_conflict")
    if child is not None and any(
        item.observed_at < child.dispatch_checkpoint_at for item in history.reconciliations
    ):
        return OrdinaryEffectRecovery("terminal", reason_code="effect_history_observation_stale")
    if record.state in {"terminal_conflict", "exhausted"} or record.reason_code in {
        "reconciliation_exhausted",
        "custody_attempts_exhausted",
    }:
        return OrdinaryEffectRecovery("terminal", reason_code=record.reason_code or record.state)
    if record.state == "not_dispatched":
        # An explicit rejection is final even if inconsistent later history
        # contains a no-effect observation. Observation cannot overturn refusal.
        if isinstance(
            history.outcome, effects.OrdinaryAgentKnownNotDispatchedOutcome
        ) and history.outcome.reason in {"provider_rejected", "non_mergeable"}:
            return OrdinaryEffectRecovery("terminal", reason_code=history.outcome.reason)
        retryable = isinstance(
            history.outcome, effects.OrdinaryAgentKnownNotDispatchedOutcome
        ) and history.outcome.reason in {
            "local_ttl",
            "transport_not_sent",
            "provider_attempt_deadline",
        }
        if history.reconciliations:
            retryable = (
                classify_effect_reconciliation(record, history.reconciliations[-1].observation)
                == "not_dispatched"
            )
        if retryable:
            if record.dispatch_count < effects.MAX_SEMANTIC_DISPATCH_ATTEMPTS_PER_EFFECT:
                return OrdinaryEffectRecovery("retry")
            return OrdinaryEffectRecovery("terminal", reason_code="effect_attempts_exhausted")
        reason = (
            history.outcome.reason
            if isinstance(history.outcome, effects.OrdinaryAgentKnownNotDispatchedOutcome)
            else record.reason_code
        )
        return OrdinaryEffectRecovery("terminal", reason_code=reason or "not_dispatched")
    if history.undispatched_completion is not None:
        completion = history.undispatched_completion
        if child is not None or history.outcome is not None:
            raise OrdinaryAgentSessionAdmissionDenied("effect_history_conflict")
        if completion.disposition == "candidate_ref_retained_no_conditional_delete":
            if (
                record.command.kind != "candidate_ref_delete"
                or record.state != "retained_no_conditional_delete"
            ):
                raise OrdinaryAgentSessionAdmissionDenied("effect_history_conflict")
            # There is no delete outcome: GitHub cannot conditionally delete the
            # ref. The router must report retention rather than fabricate success.
            return OrdinaryEffectRecovery("retained")
        if completion.disposition != "label_already_present":
            raise OrdinaryAgentSessionAdmissionDenied("effect_history_disposition_unhandled")
        if (
            record.command.kind != "stack_child_label"
            or record.state != "completed_observed"
            or completion.observation is None
            or classify_effect_reconciliation(record, completion.observation.observation)
            != "completed_observed"
        ):
            raise OrdinaryAgentSessionAdmissionDenied("effect_history_conflict")
        return OrdinaryEffectRecovery("replay", completed=effects.OrdinaryAgentCompletedOutcome())
    if record.state in {"completed", "completed_observed", "rebind_pending"}:
        outcome: effects.OrdinaryAgentCompletedOutcome | None = None
        if isinstance(history.outcome, effects.OrdinaryAgentCompletedOutcome):
            outcome = history.outcome
        elif history.reconciliations:
            observation = history.reconciliations[-1].observation
            if classify_effect_reconciliation(record, observation) not in {
                "completed_observed",
                "rebind_pending",
            }:
                raise OrdinaryAgentSessionAdmissionDenied("effect_history_conflict")
            outcome = _completed_observation(record, observation)
        if outcome is None or child is None:
            raise OrdinaryAgentSessionAdmissionDenied("effect_history_evidence_unavailable")
        # Label/close synchronous successes are command-bound acknowledgements.
        # Their stored shape carries no repeated state proof; uncertain outcomes
        # instead require typed read observations classified above. Commit-changing
        # operations retain and validate exact proof under their existing contract.
        state = require_completed_effect_proof(record, outcome)
        return OrdinaryEffectRecovery(
            "rebind" if state == "rebind_pending" and record.rebound_revision is None else "replay",
            completed=outcome,
        )
    if isinstance(history.outcome, effects.OrdinaryAgentKnownNotDispatchedOutcome):
        return OrdinaryEffectRecovery("terminal", reason_code=history.outcome.reason)
    if child is not None or record.dispatch_count or history.outcome is not None:
        return OrdinaryEffectRecovery(
            "observe",
            reason_code="provider_pending"
            if isinstance(history.outcome, effects.OrdinaryAgentAcceptedAsyncOutcome)
            else "provider_outcome_unknown",
        )
    if record.state == "reserved":
        return OrdinaryEffectRecovery("fresh")
    return OrdinaryEffectRecovery("terminal", reason_code="effect_history_evidence_unavailable")


def _completed_observation(
    record: effects.OrdinaryAgentEffectRecord,
    observation: effects.OrdinaryAgentProviderObservation,
) -> effects.OrdinaryAgentCompletedOutcome:
    if isinstance(observation, effects.OrdinaryAgentRefObservation):
        no_op = (
            record.command.kind == "candidate_head_merge"
            and observation.sha == record.command.effect.rolling_parent_sha
            and observation.contained_head_sha == record.command.effect.head_sha
        )
        return effects.OrdinaryAgentCompletedOutcome(
            result_sha=observation.sha, proof=observation, no_op=no_op
        )
    if isinstance(observation, effects.OrdinaryAgentPullRequestObservation):
        result_sha = (
            observation.head_sha
            if record.command.kind == "pull_request_head_refresh"
            else observation.merge_commit_sha
        )
        return effects.OrdinaryAgentCompletedOutcome(result_sha=result_sha, proof=observation)
    if isinstance(observation, effects.OrdinaryAgentCommentObservation):
        return effects.OrdinaryAgentCompletedOutcome(result_id=observation.matching_comment_id)
    if isinstance(observation, effects.OrdinaryAgentLabelObservation):
        # Its typed proof remains in immutable reconciliation history; the
        # semantic label method returns a command acknowledgement, not a commit.
        return effects.OrdinaryAgentCompletedOutcome()
    raise OrdinaryAgentSessionAdmissionDenied("effect_history_evidence_unavailable")
