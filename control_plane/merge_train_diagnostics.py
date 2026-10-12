"""Project bounded pass evidence without reading providers or deciding recovery."""

import hashlib
import json
from typing import Literal, cast

from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerLastResult,
    MergeTrainControllerStateRecord,
    MergeTrainObservedCheckStatus,
)


def _object(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _text(value: object, limit: int = 256) -> str:
    return value[:limit] if isinstance(value, str) else ""


def _checks(value: object) -> MergeTrainObservedCheckStatus:
    if isinstance(value, str) and value in {"unknown", "pending", "pass", "fail"}:
        return cast(MergeTrainObservedCheckStatus, value)
    return "not_observed"


def controller_last_result(
    *,
    result: dict[str, object],
    state: MergeTrainControllerStateRecord,
    observations: dict[str, str],
    pass_started_at: str,
    observed_at: str,
    trace_id: str,
    reconciliation_required: bool,
    persisted: bool,
) -> tuple[MergeTrainControllerLastResult, str]:
    candidate = _object(result.get("candidate"))
    details = _object(result.get("details"))
    action = _text(result.get("controller_action"), 128) or state.active_action or "unknown"
    reason = _text(result.get("reason_code"), 128)
    if reason == "failed_candidate_recovery_unavailable" or not reason:
        reason = observations.get("reason_code", "") or reason or action
    key = _semantic_key(result)
    previous = state.last_result
    batch_id = _text(candidate.get("batch_id"))
    same_recovery = (
        previous is not None
        and bool(batch_id)
        and previous.candidate_batch_id == batch_id
        and previous.policy_sha256 == state.policy_sha256
        and previous.policy_key == state.policy_key
    )
    recovery_kind = (
        reason
        if reason
        in {
            "failure_isolation",
            "candidate_checks_recovered",
            "infrastructure_retry",
            "closed_batch_body_changed",
        }
        else previous.recovery_kind
        if same_recovery and previous is not None
        else ""
    )
    source_failed_record_id = observations.get("source_failed_record_id", "")
    if not source_failed_record_id and same_recovery and previous is not None:
        source_failed_record_id = previous.source_failed_record_id
    if (
        recovery_kind == "failure_isolation"
        and action == "observe_candidate"
        and observations.get("candidate_checks_status") == "pending"
    ):
        reason = "failure_isolation_checks_pending"
    progress_at = previous.semantic_progress_at if previous is not None else ""
    if persisted and key and key != state.semantic_progress_key:
        progress_at = _text(candidate.get("updated_at"), 64) or state.updated_at
    actor: Literal["controller", "provider", "repository_agent"] = "controller"
    observation = "Run the next fenced controller pass with fresh evidence."
    if reconciliation_required:
        observation = "Reconcile the checkpointed effect with fresh provider evidence."
    elif reason in {"candidate_check_retry_used", "infrastructure_retry_used"}:
        actor = "repository_agent"
        observation = "Repair the failed checks; then let the controller reobserve the lineage."
    elif action in {"wait_for_checks", "observe_candidate", "wait_for_root_checks"} or reason in {
        "candidate_checks_pending",
        "candidate_failure_evidence_unavailable",
        "base_checks_not_green",
    }:
        actor = "provider"
        observation = "Observe fresh candidate, original-head or base check evidence."
    return (
        MergeTrainControllerLastResult(
            repository=state.repository,
            base_branch=state.base_branch,
            policy_key=state.policy_key,
            policy_sha256=state.policy_sha256,
            pass_started_at=pass_started_at,
            observed_at=observed_at,
            trace_id=_text(trace_id),
            action=action,
            reason_code=reason,
            record_id=_text(
                result.get("merge_train_batch_candidate_record_id")
                or result.get("merge_train_batch_landing_plan_record_id")
                or result.get("merge_train_stack_collapse_plan_record_id")
                or state.active_record_id
            ),
            source_failed_record_id=_text(source_failed_record_id),
            candidate_batch_id=batch_id,
            recovery_kind=recovery_kind,
            candidate_sha=_text(
                candidate.get("candidate_sha") or observations.get("candidate_sha"), 128
            ),
            observed_candidate_sha=_text(observations.get("candidate_sha"), 128),
            base_sha=_text(candidate.get("base_sha") or observations.get("base_sha"), 128),
            candidate_checks_status=_checks(observations.get("candidate_checks_status")),
            base_checks_status=_checks(
                details.get("base_checks_status") or observations.get("base_checks_status")
            ),
            check_retry_of=_text(
                candidate.get("check_retry_of") or observations.get("check_retry_of")
            ),
            infrastructure_retry_of=_text(
                candidate.get("infrastructure_retry_of")
                or observations.get("infrastructure_retry_of")
            ),
            batch_body_retry_of=_text(
                candidate.get("batch_body_retry_of") or observations.get("batch_body_retry_of")
            ),
            reconciliation_required=reconciliation_required,
            semantic_progress_at=progress_at,
            next_actor=actor,
            next_observation=observation,
        ),
        key or state.semantic_progress_key,
    )


def _semantic_key(result: dict[str, object]) -> str:
    # Exclude pass times, provider bodies, record IDs and phase/heartbeat churn.
    evidence: dict[str, object] = {}
    for name, fields in (
        (
            "candidate",
            (
                "batch_id",
                "base_sha",
                "candidate_sha",
                "status",
                "required_checks_status",
                "held_out",
            ),
        ),
        ("landing_plan", ("plan_id", "candidate_sha", "entries")),
        ("stack_collapse_plan", ("collapse_id", "status", "mutations", "child_dispositions")),
    ):
        value = _object(result.get(name))
        if value:
            evidence[name] = {field: value[field] for field in fields if field in value}
    if not evidence:
        return ""
    return hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
