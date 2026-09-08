from __future__ import annotations

from typing import cast

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentCredentialEvidence,
    OrdinaryAgentEligibilityResult,
    OrdinaryAgentLease,
    OrdinaryAgentPolicyEvaluation,
    OrdinaryAgentPolicyRule,
    OrdinaryAgentPolicySnapshot,
    OrdinaryAgentPrincipal,
    OrdinaryAgentReasonCode,
    OrdinaryAgentRequest,
    OrdinaryAgentSession,
    OrdinaryAgentTarget,
    PolicyDecision,
)


def _fingerprint(
    *,
    snapshot: OrdinaryAgentPolicySnapshot,
    principal: OrdinaryAgentPrincipal,
    rule: OrdinaryAgentPolicyRule | None,
    target: OrdinaryAgentTarget,
    action: str,
    managed_set_id: str,
    managed_rule_id: str,
    decision: str,
    reason_code: str,
) -> str:
    effective_inputs = {
        "principal": {
            "principal_id": principal.principal_id,
            "execution_profile": principal.execution_profile,
            "status": principal.status,
        },
        "rule": None
        if rule is None
        else {
            **rule.model_dump(mode="json"),
            "actions": sorted(rule.actions),
        },
        "target": target.model_dump(mode="json"),
        "action": action,
        "managed_set_id": managed_set_id,
        "managed_rule_id": managed_rule_id,
    }
    payload = {
        "input_domain_id": snapshot.input_domain_id,
        "evaluator_semantics_version": snapshot.evaluator_semantics_version,
        "effective_inputs": effective_inputs,
        "decision": decision,
        "reason_code": reason_code,
    }
    return f"oae-fp-v1:{canonical_json_sha256(payload)}"


def evaluate_ordinary_agent_policy(
    *,
    snapshot: OrdinaryAgentPolicySnapshot,
    principal: OrdinaryAgentPrincipal,
    target: OrdinaryAgentTarget,
    action: str,
    managed_set_id: str,
    managed_rule_id: str,
) -> OrdinaryAgentPolicyEvaluation:
    matching = tuple(
        rule
        for rule in snapshot.rules
        if rule.managed_set_id == managed_set_id and rule.managed_rule_id == managed_rule_id
    )
    rule = matching[0] if len(matching) == 1 else None
    decision: PolicyDecision
    reason: OrdinaryAgentReasonCode
    if principal.status != "active":
        decision, reason = "deny", "principal_revoked"
    elif principal.execution_profile == "read_only" and action == "guarded_merge":
        decision, reason = "deny", "principal_read_only"
    elif len(matching) == 0:
        decision, reason = "deny", "bound_rule_missing"
    elif len(matching) > 1:
        decision, reason = "deny", "bound_rule_ambiguous"
    elif rule is not None and rule.principal_id != principal.principal_id:
        decision, reason = "deny", "rule_principal_mismatch"
    elif rule is not None and rule.target != target:
        decision, reason = "deny", "rule_target_mismatch"
    elif rule is not None and action not in rule.actions:
        decision, reason = "deny", "action_not_allowed"
    else:
        decision, reason = "allow", "policy_allowed"
    fingerprint = _fingerprint(
        snapshot=snapshot,
        principal=principal,
        rule=rule,
        target=target,
        action=action,
        managed_set_id=managed_set_id,
        managed_rule_id=managed_rule_id,
        decision=decision,
        reason_code=reason,
    )
    return OrdinaryAgentPolicyEvaluation(
        record_kind="proposed_ordinary_agent_v1",
        authority_state="inert",
        authorizes_execution=False,
        record_id="ordinary-policy-evaluation-"
        + canonical_json_sha256(
            {
                "record_id": snapshot.record_id,
                "revision": snapshot.revision,
                "digest": snapshot.policy_digest,
                "fingerprint": fingerprint,
            }
        ),
        decision=decision,
        reason_code=reason,
        policy_record_id=snapshot.record_id,
        policy_revision=snapshot.revision,
        policy_digest=snapshot.policy_digest,
        managed_set_id=None if rule is None else rule.managed_set_id,
        managed_rule_id=None if rule is None else rule.managed_rule_id,
        bound_rule_actions=() if rule is None else rule.actions,
        effective_decision_fingerprint=fingerprint,
    )


def evaluate_ordinary_agent_eligibility(
    *,
    result_record_id: str,
    now: int,
    snapshot: OrdinaryAgentPolicySnapshot,
    principal: OrdinaryAgentPrincipal,
    credential: OrdinaryAgentCredentialEvidence,
    session: OrdinaryAgentSession,
    lease: OrdinaryAgentLease,
    request: OrdinaryAgentRequest,
) -> OrdinaryAgentEligibilityResult:
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise ValueError("now must be a non-negative integer")
    policy = evaluate_ordinary_agent_policy(
        snapshot=snapshot,
        principal=principal,
        target=request.target,
        action=request.action,
        managed_set_id=lease.managed_set_id,
        managed_rule_id=lease.managed_rule_id,
    )
    reason = _eligibility_reason(
        now=now,
        principal=principal,
        credential=credential,
        session=session,
        lease=lease,
        request=request,
        policy=policy,
    )
    return OrdinaryAgentEligibilityResult(
        record_kind="proposed_ordinary_agent_v1",
        authority_state="inert",
        authorizes_execution=False,
        record_id=result_record_id,
        evaluated_at=now,
        request_id=request.request_id,
        request_digest=canonical_json_sha256(request.model_dump(mode="json")),
        principal_id=principal.principal_id,
        session_id=session.session_id,
        lease_id=lease.lease_id,
        decision="eligible" if reason == "eligible" else "denied",
        reason_code=reason,
        policy_record_id=policy.policy_record_id,
        policy_revision=policy.policy_revision,
        policy_digest=policy.policy_digest,
        effective_decision_fingerprint=policy.effective_decision_fingerprint,
    )


def _eligibility_reason(
    *,
    now: int,
    principal: OrdinaryAgentPrincipal,
    credential: OrdinaryAgentCredentialEvidence,
    session: OrdinaryAgentSession,
    lease: OrdinaryAgentLease,
    request: OrdinaryAgentRequest,
    policy: OrdinaryAgentPolicyEvaluation,
) -> OrdinaryAgentReasonCode:
    # Stable refusal order: principal/profile, target, chain, time, policy, request, budget.
    if principal.status != "active":
        return "principal_revoked"
    if principal.execution_profile == "read_only" and request.action == "guarded_merge":
        return "principal_read_only"
    if lease.target != request.target:
        return "lease_target_mismatch"
    if credential.principal_id != principal.principal_id:
        return "credential_principal_mismatch"
    if session.principal_id != principal.principal_id:
        return "session_principal_mismatch"
    if lease.principal_id != principal.principal_id:
        return "lease_principal_mismatch"
    if session.credential_id != credential.credential_id:
        return "session_credential_mismatch"
    if session.credential_version != credential.credential_version:
        return "credential_version_rotated"
    if session.credential_digest != credential.credential_digest:
        return "credential_digest_rotated"
    if lease.session_id != session.session_id:
        return "lease_session_mismatch"
    if request.session_id != session.session_id or request.lease_id != lease.lease_id:
        return "request_chain_mismatch"
    if request.principal_id != principal.principal_id:
        return "request_principal_mismatch"
    if (
        not credential.valid_from <= session.valid_from
        or session.expires_at > credential.expires_at
    ):
        return "session_outside_credential_lifetime"
    if not session.valid_from <= lease.valid_from or lease.expires_at > session.expires_at:
        return "lease_outside_session_lifetime"
    for name, evidence in (("credential", credential), ("session", session), ("lease", lease)):
        if now < evidence.valid_from:
            return cast(OrdinaryAgentReasonCode, f"{name}_not_yet_valid")
        if now >= evidence.expires_at:
            return cast(OrdinaryAgentReasonCode, f"{name}_expired")
        if evidence.revoked_at is not None and evidence.revoked_at <= now:
            return cast(OrdinaryAgentReasonCode, f"{name}_revoked")
    if policy.decision != "allow":
        return policy.reason_code
    if lease.action not in policy.bound_rule_actions:
        return "lease_action_not_allowed"
    if lease.effective_decision_fingerprint != policy.effective_decision_fingerprint:
        return "effective_decision_fingerprint_mismatch"
    if request.action != lease.action:
        return "request_action_mismatch"
    if not lease.budget.window_start <= now < lease.budget.window_end:
        return "budget_window_inactive"
    if (
        lease.budget.actions_used + 1 > lease.budget.action_limit
        or lease.budget.pull_requests_used + len(request.pull_requests)
        > lease.budget.pull_request_limit
    ):
        return "budget_exhausted"
    return "eligible"
