"""Pure interpretation of exact command-bound provider evidence."""

from typing import Never

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied


def _deny() -> Never:
    raise OrdinaryAgentSessionAdmissionDenied("effect_proof_conflict")


def require_completed_effect_proof(
    record: effects.OrdinaryAgentEffectRecord,
    outcome: effects.OrdinaryAgentCompletedOutcome,
) -> effects.EffectState:
    command, proof = record.command, outcome.proof
    if proof is not None and proof.repository.lower() != record.target.repository.lower():
        _deny()
    if command.kind == "candidate_ref_prepare":
        if (
            not isinstance(proof, effects.OrdinaryAgentRefObservation)
            or proof.ref != command.effect.candidate_ref
            or proof.sha != command.effect.base_sha
        ):
            _deny()
    elif command.kind == "candidate_head_merge":
        if (
            not isinstance(proof, effects.OrdinaryAgentRefObservation)
            or proof.ref != command.effect.candidate_ref
            or not proof.tree_sha
            or not proof.sha
            or proof.sha != outcome.result_sha
        ):
            _deny()
        assert isinstance(proof, effects.OrdinaryAgentRefObservation)
        if outcome.no_op:
            if (
                proof.sha != command.effect.rolling_parent_sha
                or proof.contained_head_sha != command.effect.head_sha
            ):
                _deny()
        elif proof.parents != (command.effect.rolling_parent_sha, command.effect.head_sha):
            _deny()
    elif command.kind == "stack_child_merge":
        if (
            not isinstance(proof, effects.OrdinaryAgentRefObservation)
            or proof.ref
            not in {command.effect.parent_head_ref, "refs/heads/" + command.effect.parent_head_ref}
            or not proof.tree_sha
            or not proof.sha
            or proof.sha != outcome.result_sha
            or proof.parents
            != (command.effect.expected_parent_head_sha, command.effect.child_head_sha)
        ):
            _deny()
    elif command.kind == "pull_request_landing":
        landing_parents = (
            (command.effect.rolling_base_sha, command.effect.head_sha)
            if command.effect.merge_method == "merge"
            else (command.effect.rolling_base_sha,)
        )
        if isinstance(proof, effects.OrdinaryAgentRefObservation):
            if (
                proof.ref != "refs/heads/" + record.target.base_branch
                or not proof.sha
                or proof.sha != outcome.result_sha
                or not proof.tree_sha
                or proof.parents != landing_parents
            ):
                _deny()
        elif isinstance(proof, effects.OrdinaryAgentPullRequestObservation):
            if (
                proof.number != command.effect.pull_request_number
                or not proof.merged
                or proof.head_sha != command.effect.head_sha
                or proof.base_ref != record.target.base_branch
                or proof.merge_commit_sha != outcome.result_sha
                or not proof.merge_commit_tree_sha
                or proof.merge_commit_parents != landing_parents
            ):
                _deny()
        else:
            _deny()
    elif command.kind == "pull_request_head_refresh":
        if (
            not isinstance(proof, effects.OrdinaryAgentPullRequestObservation)
            or proof.number != command.effect.pull_request_number
            or proof.base_ref != record.target.base_branch
            or proof.base_sha != command.effect.expected_base_sha
            or not proof.head_sha
            or proof.head_sha != outcome.result_sha
            or proof.head_sha == command.effect.expected_head_sha
            or not {command.effect.expected_head_sha, command.effect.expected_base_sha}.issubset(
                proof.head_parents
            )
        ):
            _deny()
        return "rebind_pending"
    elif command.kind == "stack_child_comment":
        if not outcome.result_id:
            _deny()
    elif command.kind == "candidate_ref_delete":
        _deny()  # The provider has no conditional delete; only retained completion is valid.
    return "completed"


def classify_effect_reconciliation(
    record: effects.OrdinaryAgentEffectRecord,
    observation: effects.OrdinaryAgentProviderObservation,
) -> effects.EffectState:
    if observation.repository.lower() != record.target.repository.lower():
        _deny()
    command = record.command
    if command.kind == "candidate_ref_prepare" and isinstance(
        observation, effects.OrdinaryAgentRefObservation
    ):
        if observation.ref != command.effect.candidate_ref:
            _deny()
        return (
            "not_dispatched"
            if observation.sha is None
            else "completed_observed"
            if observation.sha == command.effect.base_sha
            else "terminal_conflict"
        )
    if command.kind == "candidate_head_merge" and isinstance(
        observation, effects.OrdinaryAgentRefObservation
    ):
        effect = command.effect
        if observation.ref != effect.candidate_ref:
            _deny()
        if observation.sha == effect.rolling_parent_sha:
            return "not_dispatched"
        message = f"Launchplane merge train {effect.lineage.batch_id}: merge PR #{effect.pull_request_number}"
        if (
            observation.sha
            and observation.tree_sha
            and observation.parents == (effect.rolling_parent_sha, effect.head_sha)
            and observation.commit_message == message
        ):
            return "completed_observed"
        return "terminal_conflict"
    if command.kind == "stack_child_merge" and isinstance(
        observation, effects.OrdinaryAgentRefObservation
    ):
        stack_effect = command.effect
        if observation.ref not in {
            stack_effect.parent_head_ref,
            "refs/heads/" + stack_effect.parent_head_ref,
        }:
            _deny()
        if observation.sha == stack_effect.expected_parent_head_sha:
            return "not_dispatched"
        message = f"Launchplane stack collapse {stack_effect.lineage.collapse_id}: merge PR #{stack_effect.child_pull_request_number} into PR #{stack_effect.parent_pull_request_number}"
        if (
            observation.sha
            and observation.tree_sha
            and observation.parents
            == (stack_effect.expected_parent_head_sha, stack_effect.child_head_sha)
            and observation.commit_message == message
        ):
            return "completed_observed"
        return "terminal_conflict"
    if command.kind == "pull_request_head_refresh" and isinstance(
        observation, effects.OrdinaryAgentPullRequestObservation
    ):
        refresh_effect = command.effect
        if (
            observation.number != refresh_effect.pull_request_number
            or observation.base_ref != record.target.base_branch
        ):
            _deny()
        if (
            observation.head_sha != refresh_effect.expected_head_sha
            and observation.base_sha == refresh_effect.expected_base_sha
            and {refresh_effect.expected_head_sha, refresh_effect.expected_base_sha}.issubset(
                observation.head_parents
            )
        ):
            return "rebind_pending"
        return "reconciliation_required"
    if command.kind == "pull_request_landing" and isinstance(
        observation, effects.OrdinaryAgentPullRequestObservation
    ):
        if observation.number != command.effect.pull_request_number:
            _deny()
        if (
            observation.head_sha != command.effect.head_sha
            or observation.base_ref != record.target.base_branch
        ):
            return "terminal_conflict"
        return (
            "completed_observed"
            if observation.merged and observation.merge_commit_sha
            else "reconciliation_required"
        )
    if command.kind == "stack_child_comment" and isinstance(
        observation, effects.OrdinaryAgentCommentObservation
    ):
        if observation.number != command.effect.pull_request_number:
            _deny()
        body = command.effect.body + "\n\n" + f"<!-- launchplane-effect:{record.effect_id} -->"
        return (
            "completed_observed"
            if observation.matching_comment_id and observation.matching_body == body
            else "reconciliation_required"
        )
    if command.kind == "stack_child_label" and isinstance(
        observation, effects.OrdinaryAgentLabelObservation
    ):
        if (
            observation.number != command.effect.pull_request_number
            or observation.label != command.effect.label
        ):
            _deny()
        return "completed_observed" if observation.present else "reconciliation_required"
    if command.kind == "stack_child_close" and isinstance(
        observation, effects.OrdinaryAgentPullRequestObservation
    ):
        if observation.number != command.effect.pull_request_number:
            _deny()
        if observation.merged or observation.head_sha != command.effect.expected_head_sha:
            return "terminal_conflict"
        return "completed_observed" if observation.state == "closed" else "reconciliation_required"
    _deny()
