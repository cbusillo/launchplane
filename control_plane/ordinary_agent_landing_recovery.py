"""Recover proven landing progress without issuing another provider operation."""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentEffectStore,
    OrdinaryAgentLandingPreparation,
    OrdinaryAgentLandingStore,
    OrdinaryAgentPullRequestObservation,
    OrdinaryAgentRefObservation,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentJobBinding,
)
from control_plane.contracts.ordinary_agent_snapshot import OrdinaryAgentLandingEvidence
from control_plane.merge_admission import GuardedMergeAdmission, MergeAdmissionDeniedError
from control_plane.ordinary_agent_effect_recovery import recover_ordinary_effect
from control_plane.ordinary_agent_landing_execution import OrdinaryLandingRecoveryRequired
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied


class OrdinaryLandingRecoveryStore(OrdinaryAgentLandingStore, OrdinaryAgentEffectStore, Protocol):
    pass


def recover_ordinary_landing_entry(
    *,
    store: OrdinaryLandingRecoveryStore,
    request: OrdinaryAgentFiniteRequestRecord,
    preparation_id: str,
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_plan_record: MergeTrainBatchLandingPlanRecord,
    guard_factory: Callable[
        [OrdinaryAgentLandingPreparation, OrdinaryAgentLandingEvidence], GuardedMergeAdmission
    ],
    checkpoint: Callable[[MergeTrainBatchLandingEntry], None],
) -> MergeTrainBatchLandingEntry:
    """Historical proof may finish progress; it never renews admission or dispatch.

    The supplied checkpoint owns current controller authority and exact successor
    persistence, just as in fresh landing. Uncertain history is left to a separate
    reconciliation poll; even a proven not-dispatched attempt is not retried here.
    """
    finalization = store.read_ordinary_landing_finalization(preparation_id=preparation_id)
    if finalization is None:
        raise OrdinaryLandingRecoveryRequired(preparation_id)
    preparation = finalization.preparation
    binding = OrdinaryAgentJobBinding(
        request_id=request.request_id,
        binding_revision=request.binding_revision,
        scope_sha256=request.scope_sha256,
    )
    plan = landing_plan_record.landing_plan
    if (
        preparation.preparation_id != preparation_id
        or preparation.state != "consumed"
        or preparation.request_id != request.request_id
        or preparation.binding_revision != request.binding_revision
        or preparation.scope_sha256 != request.scope_sha256
        or preparation.target != request.target
        or candidate_record.ordinary_job_binding != binding
        or landing_plan_record.ordinary_job_binding != binding
        or preparation.candidate_record_id != candidate_record.record_id
        or preparation.landing_plan_record_id != landing_plan_record.record_id
        or preparation.landing_plan_sha256 != plan.landing_plan_sha256
        or candidate_record.candidate.candidate_sha256 != plan.candidate_sha256
        or preparation.entry not in plan.entries
    ):
        raise OrdinaryAgentSessionAdmissionDenied("landing_history_binding_conflict")
    history = store.read_ordinary_agent_effect_history(effect_id=finalization.effect.effect_id)
    if (
        history.effect.request_id != request.request_id
        or history.effect.binding_revision != request.binding_revision
        or history.effect.scope_sha256 != request.scope_sha256
        or history.effect.target != request.target
        or history.effect.command != finalization.effect.command
        or history.effect.command_sha256 != finalization.effect.command_sha256
        or history.child != finalization.child
        or history.effect.command.kind != "pull_request_landing"
    ):
        raise OrdinaryAgentSessionAdmissionDenied("landing_history_binding_conflict")
    command = history.effect.command.effect
    if (
        command.pull_request_number != preparation.entry.pull_request_number
        or command.head_sha != preparation.entry.expected_head_sha
        or command.rolling_base_sha != preparation.expected_base_sha
    ):
        raise OrdinaryAgentSessionAdmissionDenied("landing_history_binding_conflict")
    recovery = recover_ordinary_effect(history)
    if (
        recovery.disposition != "replay"
        or recovery.completed is None
        or history.effect.state not in {"completed", "completed_observed"}
    ):
        raise OrdinaryLandingRecoveryRequired(preparation_id)
    completed = recovery.completed
    proof = completed.proof
    proof_tree = (
        proof.tree_sha
        if isinstance(proof, OrdinaryAgentRefObservation)
        else proof.merge_commit_tree_sha
        if isinstance(proof, OrdinaryAgentPullRequestObservation)
        else None
    )
    if (
        not isinstance(proof, (OrdinaryAgentPullRequestObservation, OrdinaryAgentRefObservation))
        or not completed.result_sha
        or completed.no_op
        or history.effect.dispatch_count < 1
        or preparation.evidence is None
        or proof_tree != preparation.expected_merge_tree_sha
    ):
        raise OrdinaryAgentSessionAdmissionDenied("landing_response_unproven")
    entry = preparation.entry.model_copy(
        update={
            "status": "merged",
            "landed_head_sha": preparation.entry.expected_head_sha,
            "landed_head_tree_sha": preparation.entry.expected_head_tree_sha,
            "merge_commit_sha": completed.result_sha,
            "merge_commit_tree_sha": preparation.expected_merge_tree_sha,
            "recorded_rolling_base_sha": preparation.expected_base_sha,
            "recorded_rolling_base_tree_sha": preparation.expected_base_tree_sha,
        }
    )
    guard = guard_factory(preparation, preparation.evidence)
    # Resolve the adapter's current history scope before interpreting an empty
    # outcome list. A filtered-out historical admission is not an absent outcome.
    try:
        admission = guard.record_store.read_merge_admission_record(
            finalization.admission.admission_id
        )
    except MergeAdmissionDeniedError as error:
        raise OrdinaryAgentSessionAdmissionDenied("landing_history_binding_conflict") from error
    if admission != finalization.admission:
        raise OrdinaryAgentSessionAdmissionDenied("landing_history_binding_conflict")
    # The outcome reader orders by descending observation_sequence; the adapter
    # preserves that order while applying scope and limit.
    prior = guard.record_store.list_merge_landing_outcome_records(
        admission_id=finalization.admission.admission_id,
        limit=1,
    )
    if prior and prior[0].status == "landed":
        outcome = prior[0]
        if (
            outcome.admission_binding_sha256 != finalization.admission.admission_binding_sha256
            or outcome.merge_commit_sha != entry.merge_commit_sha
            or outcome.merge_commit_tree_sha != entry.merge_commit_tree_sha
            or outcome.observed_pull_request_head_sha != entry.landed_head_sha
            or outcome.observed_pull_request_head_tree_sha != entry.landed_head_tree_sha
            or not outcome.provider_effect_attempted
            or not outcome.exact_landing_confirmed
        ):
            raise OrdinaryAgentSessionAdmissionDenied("landing_response_conflict")
    elif prior and prior[0].status != "reconcile_required":
        raise OrdinaryAgentSessionAdmissionDenied("landing_response_conflict")
    else:
        # A merged-PR observation proves historical provider completion, but does
        # not itself observe the base ref containing the merge. Preserve that
        # distinction until a separate base-proof recovery step is available.
        if not isinstance(proof, OrdinaryAgentRefObservation):
            raise OrdinaryLandingRecoveryRequired(preparation_id)
        observed_at = (
            history.reconciliations[-1].observed_at
            if history.reconciliations
            else finalization.child.dispatch_checkpoint_at
        )
        guard.record_landed(
            admission=finalization.admission,
            entry=entry,
            observed_base_sha=completed.result_sha,
            observed_base_tree_sha=preparation.expected_merge_tree_sha,
            base_contains_merge_commit=True,
            provider_effect_attempted=True,
            observed_at=datetime.fromtimestamp(observed_at, timezone.utc).isoformat(),
        )
    checkpoint(entry)
    return entry
