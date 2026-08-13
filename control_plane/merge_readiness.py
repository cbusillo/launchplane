from __future__ import annotations

from datetime import UTC, datetime

from control_plane.contracts.advisory_check_projection import is_launchplane_projected_check
from control_plane.contracts.engineering_review_decision import EngineeringReviewDecisionRecord
from control_plane.contracts.engineering_review_run import EngineeringReviewRunRecord
from control_plane.contracts.merge_readiness import (
    MergeReadinessAdvisoryObservation,
    MergeReadinessAuthorityMode,
    MergeReadinessCandidateEvidence,
    MergeReadinessCandidateFacet,
    MergeReadinessEngineeringEvidenceReference,
    MergeReadinessEngineeringReviewFacet,
    MergeReadinessFenceEvidence,
    MergeReadinessFenceFacet,
    MergeReadinessOwnerFacet,
    MergeReadinessPolicyDimension,
    MergeReadinessPolicyFacet,
    MergeReadinessPolicyFingerprints,
    MergeReadinessReasonCode,
    MergeReadinessResult,
    MergeReadinessState,
    MergeReadinessStructuralCandidateStatus,
    MergeReadinessTarget,
    MergeReadinessTechnicalCheckEvidence,
    MergeReadinessTechnicalCheckStatus,
    MergeReadinessTechnicalChecksFacet,
    merge_readiness_worst_state,
)
from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceDecision,
    OwnerAcceptanceProductDecision,
    OwnerAcceptanceReasonCode,
)
from control_plane.tenant_admission_controller import (
    TenantAdmissionRequiredTechnicalCheck,
    TenantAdmissionTechnicalCheckSignal,
    TenantAdmissionTechnicalChecks,
)


_OWNER_REASON_CODES: dict[OwnerAcceptanceReasonCode, MergeReadinessReasonCode] = {
    "engineering_only": "owner_not_required",
    "acceptance_missing": "owner_acceptance_missing",
    "acceptance_valid": "owner_acceptance_valid",
    "changes_requested": "owner_changes_requested",
    "acceptance_revoked": "owner_acceptance_revoked",
    "acceptance_stale": "owner_acceptance_stale",
    "change_impact_unavailable": "owner_evidence_unavailable",
    "change_impact_stale": "owner_evidence_stale",
    "multi_product_unsupported": "owner_evidence_unavailable",
    "owner_authority_unavailable": "owner_authority_unavailable",
    "owner_authority_denied": "owner_authority_denied",
    "preview_evidence_unavailable": "owner_preview_evidence_unavailable",
    "preview_evidence_stale": "owner_preview_evidence_stale",
    "owner_review_expired": "owner_review_expired",
    "preview_isolation_insufficient": "owner_preview_isolation_insufficient",
    "contributing_identity_unknown": "owner_contributing_identity_unknown",
    "self_review_denied": "owner_self_review_denied",
    "review_context_missing": "owner_review_context_missing",
}
_POLICY_REASON_CODES: dict[
    MergeReadinessPolicyDimension,
    tuple[MergeReadinessReasonCode, MergeReadinessReasonCode],
] = {
    "impact": ("policy_impact_missing", "policy_impact_drift"),
    "technical_checks": (
        "policy_technical_checks_missing",
        "policy_technical_checks_drift",
    ),
    "engineering_review": (
        "policy_engineering_review_missing",
        "policy_engineering_review_drift",
    ),
    "ruleset": ("policy_ruleset_missing", "policy_ruleset_drift"),
    "merge_train": ("policy_merge_train_missing", "policy_merge_train_drift"),
    "authorization": ("policy_authorization_missing", "policy_authorization_drift"),
    "admission_algorithm": (
        "policy_admission_algorithm_missing",
        "policy_admission_algorithm_drift",
    ),
}

_NO_OWNER_PRODUCT_SUBJECT = "__not_applicable__"


def evaluate_merge_readiness(
    *,
    target: MergeReadinessTarget,
    owner_decision: OwnerAcceptanceDecision,
    technical_checks: MergeReadinessTechnicalCheckEvidence,
    engineering_decision: EngineeringReviewDecisionRecord | None,
    engineering_evidence: tuple[MergeReadinessEngineeringEvidenceReference, ...],
    engineering_review_authority: MergeReadinessAuthorityMode = "required",
    policy_fingerprints: MergeReadinessPolicyFingerprints,
    candidate_evidence: MergeReadinessCandidateEvidence,
    fence_evidence: MergeReadinessFenceEvidence,
    evaluated_at: str,
) -> MergeReadinessResult:
    owner_facets = _owner_facets(target=target, decision=owner_decision)
    technical_facet = _technical_checks_facet(target=target, evidence=technical_checks)
    engineering_facet = _engineering_review_facet(
        target=target,
        decision=engineering_decision,
        evidence=engineering_evidence,
    )
    policy_facet = _policy_facet(
        policy_fingerprints,
        advisory_dimensions=(
            ("engineering_review",) if engineering_review_authority == "advisory" else ()
        ),
    )
    candidate_facet = _candidate_facet(target=target, evidence=candidate_evidence)
    fence_facet = _fence_facet(
        target=target,
        evidence=fence_evidence,
        evaluated_at=evaluated_at,
    )
    facet_states = (
        *(facet.state for facet in owner_facets),
        technical_facet.state,
        *(() if engineering_review_authority == "advisory" else (engineering_facet.state,)),
        policy_facet.state,
        candidate_facet.state,
        fence_facet.state,
    )
    reason_codes = tuple(
        sorted(
            {
                reason
                for reasons in (
                    *(facet.reason_codes for facet in owner_facets),
                    technical_facet.reason_codes,
                    engineering_facet.reason_codes,
                    policy_facet.reason_codes,
                    candidate_facet.reason_codes,
                    fence_facet.reason_codes,
                )
                for reason in reasons
            }
        )
    )
    return MergeReadinessResult(
        engineering_review_authority=engineering_review_authority,
        target=target,
        state=merge_readiness_worst_state(facet_states),
        reason_codes=reason_codes,
        owner_facets=owner_facets,
        technical_checks=technical_facet,
        engineering_review=engineering_facet,
        policy=policy_facet,
        candidate=candidate_facet,
        fence=fence_facet,
        evaluated_at=evaluated_at,
    )


def evaluate_merge_readiness_from_live_evidence(
    *,
    target: MergeReadinessTarget,
    owner_decision: OwnerAcceptanceDecision,
    engineering_decision: EngineeringReviewDecisionRecord | None,
    engineering_runs: tuple[EngineeringReviewRunRecord, ...],
    engineering_review_authority: MergeReadinessAuthorityMode = "required",
    technical_checks: TenantAdmissionTechnicalChecks | None,
    policy_fingerprints: MergeReadinessPolicyFingerprints,
    candidate_record: MergeTrainBatchCandidateRecord | None,
    structural_candidate_status: MergeReadinessStructuralCandidateStatus,
    controller_state: MergeTrainControllerStateRecord | None,
    expected_lease_owner: str,
    observed_effect_sha: str,
    evaluated_at: str,
) -> MergeReadinessResult:
    """Adapt current stored/live evidence into the pure ephemeral L2 evaluator.

    The adapter performs no writes and does not create L3 authority. Structural
    composition remains owned by #2084 and enters only as its canonical status.
    """
    return evaluate_merge_readiness(
        target=target,
        owner_decision=owner_decision,
        technical_checks=_technical_check_evidence(
            target=target,
            evidence=technical_checks,
        ),
        engineering_decision=engineering_decision,
        engineering_evidence=_engineering_evidence(
            decision=engineering_decision,
            runs=engineering_runs,
        ),
        engineering_review_authority=engineering_review_authority,
        policy_fingerprints=policy_fingerprints,
        candidate_evidence=_candidate_evidence(
            target=target,
            record=candidate_record,
            structural_status=structural_candidate_status,
        ),
        fence_evidence=_fence_evidence(
            controller_state=controller_state,
            expected_lease_owner=expected_lease_owner,
            observed_effect_sha=observed_effect_sha,
        ),
        evaluated_at=evaluated_at,
    )


def _owner_facets(
    *,
    target: MergeReadinessTarget,
    decision: OwnerAcceptanceDecision,
) -> tuple[MergeReadinessOwnerFacet, ...]:
    products = decision.products
    if not products:
        if decision.binding is not None:
            products = (
                OwnerAcceptanceProductDecision(
                    product=decision.binding.product,
                    system=decision.binding.system,
                    action=decision.binding.action,
                    environment=decision.binding.environment,
                    status=decision.status,
                    reason_code=decision.reason_code,
                    binding=decision.binding,
                    current_event=decision.current_event,
                    admissible=decision.admissible,
                    human_action_semantics=decision.human_action_semantics,
                ),
            )
        else:
            return (_owner_unscoped_facet(decision),)
    return tuple(
        sorted(
            (_owner_facet(target=target, product=product) for product in products),
            key=lambda item: (item.product, item.system, item.action, item.environment),
        )
    )


def _owner_unscoped_facet(decision: OwnerAcceptanceDecision) -> MergeReadinessOwnerFacet:
    reason = _OWNER_REASON_CODES[decision.reason_code]
    reasons: tuple[MergeReadinessReasonCode, ...]
    if decision.status == "not_required":
        state: MergeReadinessState = "ready"
        reasons = ("owner_not_required",)
    elif decision.status == "unavailable":
        state = "unknown"
        reasons = tuple(sorted({reason, "owner_evidence_unavailable"}))
    else:
        state = "blocked_owner_evidence"
        reasons = (reason,)
    return MergeReadinessOwnerFacet(
        product=_NO_OWNER_PRODUCT_SUBJECT,
        system=_NO_OWNER_PRODUCT_SUBJECT,
        action=_NO_OWNER_PRODUCT_SUBJECT,
        environment=_NO_OWNER_PRODUCT_SUBJECT,
        owner_status=decision.status,
        owner_reason_code=decision.reason_code,
        state=state,
        reason_codes=reasons,
    )


def _owner_facet(
    *,
    target: MergeReadinessTarget,
    product: OwnerAcceptanceProductDecision,
) -> MergeReadinessOwnerFacet:
    reasons: list[MergeReadinessReasonCode] = [_OWNER_REASON_CODES[product.reason_code]]
    ready = product.status == "not_required" or (
        product.status == "accepted" and product.admissible
    )
    binding = product.binding
    if binding is None and product.status != "not_required":
        reasons.append("owner_evidence_unavailable")
        ready = False
    elif binding is not None:
        if binding.head_sha != target.pull_request_head_sha:
            reasons.append("owner_evidence_head_mismatch")
            ready = False
        if binding.tree_sha != target.pull_request_tree_sha:
            reasons.append("owner_evidence_tree_mismatch")
            ready = False
    return MergeReadinessOwnerFacet(
        product=product.product,
        system=product.system,
        action=product.action,
        environment=product.environment,
        owner_status=product.status,
        owner_reason_code=product.reason_code,
        binding_sha256=binding.binding_sha256 if binding is not None else "",
        event_id=product.current_event.event_id if product.current_event is not None else "",
        state="ready" if ready else "blocked_owner_evidence",
        reason_codes=tuple(reasons),
    )


def _technical_checks_facet(
    *,
    target: MergeReadinessTarget,
    evidence: MergeReadinessTechnicalCheckEvidence,
) -> MergeReadinessTechnicalChecksFacet:
    reasons: list[MergeReadinessReasonCode] = []
    states: list[MergeReadinessState] = []
    if evidence.status == "pass":
        reasons.append("checks_passed")
        states.append("ready")
    elif evidence.status == "pending":
        reasons.append("checks_pending")
        states.append("blocked_checks")
    elif evidence.status == "fail":
        reasons.append("checks_failed")
        states.append("blocked_checks")
    else:
        reasons.append("checks_unknown")
        states.append("unknown")
    if evidence.head_sha != target.expected_effect_sha:
        reasons.append("checks_head_mismatch")
        states.append("blocked_checks")
    return MergeReadinessTechnicalChecksFacet(
        head_sha=evidence.head_sha,
        status=evidence.status,
        required_checks=evidence.required_checks,
        advisory_observations=evidence.advisory_observations,
        state=merge_readiness_worst_state(tuple(states)),
        reason_codes=tuple(reasons),
    )


def _engineering_review_facet(
    *,
    target: MergeReadinessTarget,
    decision: EngineeringReviewDecisionRecord | None,
    evidence: tuple[MergeReadinessEngineeringEvidenceReference, ...],
) -> MergeReadinessEngineeringReviewFacet:
    if decision is None:
        return MergeReadinessEngineeringReviewFacet(
            status="unknown",
            state="unknown",
            reason_codes=("engineering_review_unknown", "engineering_review_evidence_missing"),
        )
    reasons: list[MergeReadinessReasonCode] = []
    states: list[MergeReadinessState] = []
    status_reason: tuple[MergeReadinessState, MergeReadinessReasonCode]
    if decision.status == "approved":
        status_reason = ("ready", "engineering_review_approved")
    elif decision.status == "pending":
        status_reason = ("blocked_engineering_review", "engineering_review_pending")
    elif decision.status == "changes_requested":
        status_reason = (
            "blocked_engineering_review",
            "engineering_review_changes_requested",
        )
    elif decision.status == "blocked":
        status_reason = ("blocked_engineering_review", "engineering_review_blocked")
    elif decision.status == "stale":
        status_reason = ("blocked_engineering_review", "engineering_review_stale")
    else:
        status_reason = ("unknown", "engineering_review_unknown")
    states.append(status_reason[0])
    reasons.append(status_reason[1])
    if decision.target.head_sha != target.pull_request_head_sha:
        states.append("blocked_engineering_review")
        reasons.append("engineering_review_head_mismatch")
    if decision.target.tree_sha != target.pull_request_tree_sha:
        states.append("blocked_engineering_review")
        reasons.append("engineering_review_tree_mismatch")
    evidence_by_id = {item.run_id: item for item in evidence}
    qualifying_evidence = tuple(
        evidence_by_id[run_id] for run_id in decision.qualifying_run_ids if run_id in evidence_by_id
    )
    if decision.status == "approved":
        evidence_complete = len(qualifying_evidence) == len(decision.qualifying_run_ids)
        evidence_exact = all(
            item.head_sha == target.pull_request_head_sha
            and item.tree_sha == target.pull_request_tree_sha
            for item in qualifying_evidence
        )
        if not evidence_complete or not evidence_exact:
            states.append("unknown")
            reasons.append("engineering_review_evidence_missing")
    return MergeReadinessEngineeringReviewFacet(
        status=decision.status,
        decision_id=decision.decision_id,
        decision_binding_sha256=decision.decision_binding_sha256,
        head_sha=decision.target.head_sha,
        tree_sha=decision.target.tree_sha,
        evidence=qualifying_evidence,
        state=merge_readiness_worst_state(tuple(states)),
        reason_codes=tuple(reasons),
    )


def _policy_facet(
    fingerprints: MergeReadinessPolicyFingerprints,
    *,
    advisory_dimensions: tuple[MergeReadinessPolicyDimension, ...] = (),
) -> MergeReadinessPolicyFacet:
    reasons: list[MergeReadinessReasonCode] = []
    blocking_reasons: list[MergeReadinessReasonCode] = []
    for fingerprint in fingerprints.ordered():
        missing_reason, drift_reason = _POLICY_REASON_CODES[fingerprint.dimension]
        if fingerprint.current_sha256 is None:
            reasons.append(missing_reason)
            if fingerprint.dimension not in advisory_dimensions:
                blocking_reasons.append(missing_reason)
        elif fingerprint.current_sha256 != fingerprint.expected_sha256:
            reasons.append(drift_reason)
            if fingerprint.dimension not in advisory_dimensions:
                blocking_reasons.append(drift_reason)
    if not reasons:
        reasons.append("policy_fingerprints_match")
    return MergeReadinessPolicyFacet(
        fingerprints=fingerprints,
        state="ready" if not blocking_reasons else "blocked_policy",
        reason_codes=tuple(reasons),
    )


def _candidate_facet(
    *,
    target: MergeReadinessTarget,
    evidence: MergeReadinessCandidateEvidence,
) -> MergeReadinessCandidateFacet:
    reasons: list[MergeReadinessReasonCode] = []
    states: list[MergeReadinessState] = []
    if evidence.structural_status == "exact":
        reasons.append("candidate_exact")
        states.append("ready")
    elif evidence.structural_status == "recorded_rolling":
        reasons.append("candidate_recorded_rolling")
        states.append("ready")
    elif evidence.structural_status == "mismatch":
        reasons.append("candidate_identity_mismatch")
        states.append("blocked_candidate_identity")
    else:
        reasons.append("candidate_identity_unknown")
        states.append("unknown")
    if not evidence.record_id:
        if "candidate_identity_unknown" not in reasons:
            reasons.append("candidate_identity_unknown")
        states.append("unknown")
    else:
        if (
            evidence.repository != target.repository
            or evidence.candidate_sha != target.expected_effect_sha
        ):
            reasons.append("candidate_identity_mismatch")
            states.append("blocked_candidate_identity")
        if (
            evidence.pull_request_number != target.pull_request_number
            or evidence.queue_position != target.queue_position
        ):
            reasons.append("candidate_queue_mismatch")
            states.append("blocked_candidate_identity")
        if evidence.pull_request_head_sha != target.pull_request_head_sha:
            reasons.append("candidate_head_mismatch")
            states.append("blocked_candidate_identity")
        if evidence.structural_status == "exact" and evidence.base_sha != target.base_sha:
            reasons.append("candidate_base_mismatch")
            states.append("blocked_candidate_identity")
    return MergeReadinessCandidateFacet(
        evidence=evidence,
        state=merge_readiness_worst_state(tuple(states)),
        reason_codes=tuple(reasons),
    )


def _fence_facet(
    *,
    target: MergeReadinessTarget,
    evidence: MergeReadinessFenceEvidence,
    evaluated_at: str,
) -> MergeReadinessFenceFacet:
    reasons: list[MergeReadinessReasonCode] = []
    states: list[MergeReadinessState] = []
    if evidence.controller_repository and (
        evidence.controller_repository != target.repository
        or evidence.controller_base_branch != target.base_branch
    ):
        reasons.append("controller_scope_mismatch")
        states.append("blocked_candidate_identity")
    if evidence.controller_status != "running" or not evidence.observed_lease_owner:
        reasons.append("controller_lease_missing")
        states.append("blocked_candidate_identity")
    elif evidence.observed_lease_owner != evidence.expected_lease_owner:
        reasons.append("controller_lease_lost")
        states.append("blocked_candidate_identity")
    elif not evidence.lease_expires_at:
        reasons.append("controller_lease_missing")
        states.append("blocked_candidate_identity")
    elif _parse_timestamp(evidence.lease_expires_at) <= _parse_timestamp(evaluated_at):
        reasons.append("controller_lease_expired")
        states.append("blocked_candidate_identity")
    else:
        reasons.append("controller_lease_held")
        states.append("ready")
    if evidence.observed_effect_sha != target.expected_effect_sha:
        reasons.append("expected_sha_mismatch")
        states.append("blocked_candidate_identity")
    else:
        reasons.append("expected_sha_match")
        states.append("ready")
    return MergeReadinessFenceFacet(
        evidence=evidence,
        state=merge_readiness_worst_state(tuple(states)),
        reason_codes=tuple(reasons),
    )


def _engineering_evidence(
    *,
    decision: EngineeringReviewDecisionRecord | None,
    runs: tuple[EngineeringReviewRunRecord, ...],
) -> tuple[MergeReadinessEngineeringEvidenceReference, ...]:
    if decision is None:
        return ()
    qualifying_ids = set(decision.qualifying_run_ids)
    return tuple(
        sorted(
            (
                MergeReadinessEngineeringEvidenceReference(
                    run_id=run.run_id,
                    head_sha=run.head_sha,
                    tree_sha=run.tree_sha,
                    evidence_digest=run.evidence_digest,
                )
                for run in runs
                if run.run_id in qualifying_ids and run.state == "completed" and run.evidence_digest
            ),
            key=lambda item: item.run_id,
        )
    )


def _technical_check_evidence(
    *,
    target: MergeReadinessTarget,
    evidence: TenantAdmissionTechnicalChecks | None,
) -> MergeReadinessTechnicalCheckEvidence:
    if evidence is None:
        return MergeReadinessTechnicalCheckEvidence(
            head_sha=target.expected_effect_sha,
            status="unknown",
        )
    required_checks = tuple(
        check
        for check in evidence.required_checks
        if not is_launchplane_projected_check(check.name)
    )
    signals = tuple(
        signal for signal in evidence.signals if not is_launchplane_projected_check(signal.name)
    )
    advisory_observations = tuple(
        MergeReadinessAdvisoryObservation(
            name=signal.name,
            state=signal.state,
            app_id=signal.app_id,
        )
        for signal in evidence.signals
        if is_launchplane_projected_check(signal.name)
    )
    status = _required_technical_check_state(
        required_checks=required_checks,
        signals=signals,
        strict=evidence.strict,
        base_up_to_date=evidence.base_up_to_date,
    )
    return MergeReadinessTechnicalCheckEvidence(
        head_sha=evidence.head_sha,
        status=status,
        required_checks=tuple(_required_check_identity(check) for check in required_checks),
        advisory_observations=advisory_observations,
    )


def _candidate_evidence(
    *,
    target: MergeReadinessTarget,
    record: MergeTrainBatchCandidateRecord | None,
    structural_status: MergeReadinessStructuralCandidateStatus,
) -> MergeReadinessCandidateEvidence:
    if record is None:
        return MergeReadinessCandidateEvidence(structural_status=structural_status)
    candidate = record.candidate
    entry = next(
        (
            item
            for item in candidate.entries
            if item.pull_request_number == target.pull_request_number
        ),
        None,
    )
    return MergeReadinessCandidateEvidence(
        structural_status=structural_status,
        record_id=record.record_id,
        repository=candidate.repository,
        base_sha=candidate.base_sha,
        candidate_sha=candidate.candidate_sha,
        pull_request_number=entry.pull_request_number if entry is not None else None,
        queue_position=entry.position if entry is not None else None,
        pull_request_head_sha=entry.head_sha if entry is not None else "",
    )


def _fence_evidence(
    *,
    controller_state: MergeTrainControllerStateRecord | None,
    expected_lease_owner: str,
    observed_effect_sha: str,
) -> MergeReadinessFenceEvidence:
    if controller_state is None:
        return MergeReadinessFenceEvidence(
            expected_lease_owner=expected_lease_owner,
            observed_effect_sha=observed_effect_sha,
        )
    return MergeReadinessFenceEvidence(
        controller_key=controller_state.controller_key,
        controller_repository=controller_state.repository,
        controller_base_branch=controller_state.base_branch,
        controller_status=controller_state.status,
        expected_lease_owner=expected_lease_owner,
        observed_lease_owner=controller_state.lease_owner,
        lease_expires_at=controller_state.lease_expires_at,
        observed_effect_sha=observed_effect_sha,
    )


def _required_check_identity(check: TenantAdmissionRequiredTechnicalCheck) -> str:
    if check.app_id is None:
        return check.name
    return f"{check.name}@app:{check.app_id}"


def _required_technical_check_state(
    *,
    required_checks: tuple[TenantAdmissionRequiredTechnicalCheck, ...],
    signals: tuple[TenantAdmissionTechnicalCheckSignal, ...],
    strict: bool,
    base_up_to_date: bool | None,
) -> MergeReadinessTechnicalCheckStatus:
    if not required_checks:
        return "unknown"
    if strict and not base_up_to_date:
        return "fail"
    required_states: list[MergeReadinessTechnicalCheckStatus] = []
    for required_check in required_checks:
        matches = tuple(
            signal
            for signal in signals
            if signal.name.casefold() == required_check.name.casefold()
            and (required_check.app_id is None or signal.app_id == required_check.app_id)
        )
        required_states.append(_combined_signal_state(matches))
    if "fail" in required_states:
        return "fail"
    if "pending" in required_states:
        return "pending"
    if "unknown" in required_states:
        return "unknown"
    return "pass"


def _combined_signal_state(
    signals: tuple[TenantAdmissionTechnicalCheckSignal, ...],
) -> MergeReadinessTechnicalCheckStatus:
    if not signals:
        return "unknown"
    states = {signal.state for signal in signals}
    if "fail" in states:
        return "fail"
    if "pending" in states:
        return "pending"
    if "unavailable" in states:
        return "unknown"
    return "pass"


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Merge readiness timestamps require timezone")
    return parsed.astimezone(UTC)
