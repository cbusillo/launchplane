"""Pure lifecycle calculations consumed by the joined ordinary admission transaction.

These functions perform no authentication, persistence or provider dispatch. The
store must verify the issuer proof/approved operation and serialize their inputs.
"""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentBudget,
    OrdinaryAgentCredentialEvidence,
    OrdinaryAgentEligibilityResult,
    OrdinaryAgentLease,
    OrdinaryAgentPolicySnapshot,
    OrdinaryAgentPrincipal,
    OrdinaryAgentPullRequest,
    OrdinaryAgentRequest,
    OrdinaryAgentSession,
    OrdinaryAgentTarget,
)
from control_plane.contracts.ordinary_agent_lifecycle import (
    OrdinaryAgentAuthenticationCredentialRecord,
    OrdinaryAgentPrincipalRecord,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentLeaseRecord,
    OrdinaryAgentSessionDelegation,
    OrdinaryAgentSessionRecord,
)
from control_plane.ordinary_agent_eligibility import (
    evaluate_ordinary_agent_eligibility,
    evaluate_ordinary_agent_policy,
    ordinary_agent_chain_reason,
    ordinary_agent_effective_authority_reason,
)


class OrdinaryAgentSessionAdmissionDenied(ValueError):
    def __init__(self, reason_code: str, *, retry_not_before: int | None = None) -> None:
        self.retry_not_before = retry_not_before
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class OrdinaryAgentSessionWriteSet:
    session: OrdinaryAgentSessionRecord
    leases: tuple[OrdinaryAgentLeaseRecord, ...]


@dataclass(frozen=True)
class OrdinaryAgentRequestAdmissionWriteSet:
    lease: OrdinaryAgentLeaseRecord
    request: OrdinaryAgentFiniteRequestRecord
    evaluation: OrdinaryAgentEligibilityResult


def _policy_inputs(
    policy: LaunchplaneAuthzPolicyRecord,
    principal: OrdinaryAgentPrincipalRecord,
) -> tuple[OrdinaryAgentPolicySnapshot, OrdinaryAgentPrincipal]:
    if policy.status != "active" or policy.policy.schema_version != 3:
        raise OrdinaryAgentSessionAdmissionDenied("policy_unavailable")
    snapshot = OrdinaryAgentPolicySnapshot(
        record_kind="proposed_ordinary_agent_v1",
        authority_state="inert",
        authorizes_execution=False,
        record_id=policy.record_id,
        revision=policy.revision,
        policy_digest=policy.policy_sha256,
        input_domain_id="ordinary-agent-effective-inputs-v1",
        evaluator_semantics_version="ordinary-agent-eligibility-v1",
        rules=policy.policy.ordinary_agents,
    )
    subject = OrdinaryAgentPrincipal(
        record_kind="proposed_ordinary_agent_v1",
        authority_state="inert",
        authorizes_execution=False,
        record_id=principal.record_id,
        principal_id=principal.principal_id,
        execution_profile=principal.execution_profile,
        status=principal.status,
    )
    return snapshot, subject


def _require_current_credential(
    principal: OrdinaryAgentPrincipalRecord,
    credential: OrdinaryAgentAuthenticationCredentialRecord,
    now: int,
) -> None:
    if (
        credential.principal_id != principal.principal_id
        or credential.credential_id != principal.credential_id
        or credential.credential_version != principal.credential_version
        or credential.credential_digest != principal.credential_digest
        or credential.policy != principal.policy
    ):
        raise OrdinaryAgentSessionAdmissionDenied("credential_binding_mismatch")
    if credential.status != "active" or not credential.valid_from <= now < credential.expires_at:
        raise OrdinaryAgentSessionAdmissionDenied("credential_unavailable")


def require_ordinary_agent_reconciliation_authority(
    *,
    policy: LaunchplaneAuthzPolicyRecord,
    principal: OrdinaryAgentPrincipalRecord,
    credential: OrdinaryAgentAuthenticationCredentialRecord,
    target: OrdinaryAgentTarget,
    now: int,
) -> None:
    """Current preflight ceiling only; never renew an old session or authorize a write."""
    _require_current_credential(principal, credential, now)
    snapshot, subject = _policy_inputs(policy, principal)
    result = evaluate_ordinary_agent_policy(
        snapshot=snapshot,
        principal=subject,
        target=target,
        action="preflight",
        managed_set_id=principal.policy.managed_set_id,
        managed_rule_id=principal.policy.managed_rule_id,
    )
    if result.decision != "allow":
        raise OrdinaryAgentSessionAdmissionDenied(result.reason_code)


def build_ordinary_agent_session_write_set(
    *,
    policy: LaunchplaneAuthzPolicyRecord,
    principal: OrdinaryAgentPrincipalRecord,
    credential: OrdinaryAgentAuthenticationCredentialRecord,
    delegation: OrdinaryAgentSessionDelegation,
    now: int,
) -> OrdinaryAgentSessionWriteSet:
    """Derive one session per approved operation and a lease per approved action."""
    _require_current_credential(principal, credential, now)
    snapshot, subject = _policy_inputs(policy, principal)
    if (
        not now
        < delegation.lease_expires_at
        <= delegation.session_expires_at
        <= credential.expires_at
    ):
        raise OrdinaryAgentSessionAdmissionDenied("delegation_lifetime_outside_credential")
    if (
        delegation.continuation_expires_at is not None
        and delegation.continuation_expires_at > credential.expires_at
    ):
        raise OrdinaryAgentSessionAdmissionDenied("continuation_outside_credential")
    identity_digest = canonical_json_sha256(
        {
            "operation_id": delegation.operation_id,
            "principal_id": principal.principal_id,
            "credential_id": credential.credential_id,
            "credential_version": credential.credential_version,
        }
    )
    session = OrdinaryAgentSessionRecord(
        session_id=f"ordinary-session-{identity_digest[:32]}",
        principal_id=principal.principal_id,
        credential_id=credential.credential_id,
        credential_version=credential.credential_version,
        credential_digest=credential.credential_digest,
        delegation=delegation,
        valid_from=now,
        expires_at=delegation.session_expires_at,
    )
    leases = []
    for action in delegation.actions:
        decision = evaluate_ordinary_agent_policy(
            snapshot=snapshot,
            principal=subject,
            target=principal.policy.target,
            action=action,
            managed_set_id=principal.policy.managed_set_id,
            managed_rule_id=principal.policy.managed_rule_id,
        )
        if decision.decision != "allow":
            raise OrdinaryAgentSessionAdmissionDenied(decision.reason_code)
        leases.append(
            OrdinaryAgentLeaseRecord(
                lease_id=f"ordinary-lease-{identity_digest[:32]}-{action}",
                session_id=session.session_id,
                principal_id=principal.principal_id,
                target=principal.policy.target,
                action=action,
                managed_set_id=principal.policy.managed_set_id,
                managed_rule_id=principal.policy.managed_rule_id,
                effective_decision_fingerprint=decision.effective_decision_fingerprint,
                valid_from=now,
                expires_at=delegation.lease_expires_at,
                budget=OrdinaryAgentBudget(
                    window_start=now,
                    window_end=delegation.continuation_expires_at or delegation.lease_expires_at,
                    action_limit=delegation.action_limit,
                    actions_used=0,
                    pull_request_limit=delegation.pull_request_limit,
                    pull_requests_used=0,
                ),
            )
        )
    return OrdinaryAgentSessionWriteSet(session=session, leases=tuple(leases))


def build_ordinary_agent_request_admission_write_set(
    *,
    policy: LaunchplaneAuthzPolicyRecord,
    principal: OrdinaryAgentPrincipalRecord,
    credential: OrdinaryAgentAuthenticationCredentialRecord,
    session: OrdinaryAgentSessionRecord,
    lease: OrdinaryAgentLeaseRecord,
    request: OrdinaryAgentFiniteRequestRecord,
    now: int,
) -> OrdinaryAgentRequestAdmissionWriteSet:
    """Calculate first admission only; the store resolves idempotency before spending."""
    _require_current_credential(principal, credential, now)
    snapshot, subject = _policy_inputs(policy, principal)
    if (
        request.status != "waiting"
        or request.binding_revision != 1
        or request.refresh_used != 0
        or request.cancellation_requested_at is not None
        or request.execution_record_ids
        or request.admitted_at != now
        or request.expires_at > lease.expires_at
        or (
            request.continuation_expires_at is not None
            and (
                session.delegation.continuation_expires_at is None
                or request.continuation_expires_at > session.delegation.continuation_expires_at
            )
        )
        or request.refresh_allowance_total > session.delegation.refresh_allowance
        or lease.target != principal.policy.target
        or lease.managed_set_id != principal.policy.managed_set_id
        or lease.managed_rule_id != principal.policy.managed_rule_id
        or lease.budget.window_start != lease.valid_from
        or lease.budget.window_end
        != (session.delegation.continuation_expires_at or lease.expires_at)
        or lease.action not in session.delegation.actions
        or lease.expires_at != session.delegation.lease_expires_at
        or lease.budget.action_limit != session.delegation.action_limit
        or lease.budget.pull_request_limit != session.delegation.pull_request_limit
    ):
        raise OrdinaryAgentSessionAdmissionDenied("request_outside_delegation")
    credential_evidence, session_evidence, lease_evidence, request_evidence = _eligibility_evidence(
        credential=credential, session=session, lease=lease, request=request
    )
    evaluation = evaluate_ordinary_agent_eligibility(
        result_record_id=f"ordinary-admission-{request.request_id}",
        now=now,
        snapshot=snapshot,
        principal=subject,
        credential=credential_evidence,
        session=session_evidence,
        lease=lease_evidence,
        request=request_evidence,
    )
    if evaluation.decision != "eligible":
        raise OrdinaryAgentSessionAdmissionDenied(evaluation.reason_code)
    budget = lease.budget.model_copy(
        update={
            "pull_requests_used": lease.budget.pull_requests_used + len(request.pull_requests),
        }
    )
    updated_lease = OrdinaryAgentLeaseRecord.model_validate(
        {
            **lease.model_dump(),
            "revision": lease.revision + 1,
            "budget": budget,
        }
    )
    return OrdinaryAgentRequestAdmissionWriteSet(
        lease=updated_lease, request=request, evaluation=evaluation
    )


def _eligibility_evidence(
    *,
    credential: OrdinaryAgentAuthenticationCredentialRecord,
    session: OrdinaryAgentSessionRecord,
    lease: OrdinaryAgentLeaseRecord,
    request: OrdinaryAgentFiniteRequestRecord,
) -> tuple[
    OrdinaryAgentCredentialEvidence, OrdinaryAgentSession, OrdinaryAgentLease, OrdinaryAgentRequest
]:
    credential_evidence = OrdinaryAgentCredentialEvidence(
        record_kind="proposed_ordinary_agent_v1",
        authority_state="inert",
        authorizes_execution=False,
        record_id=credential.record_id,
        credential_id=credential.credential_id,
        credential_version=credential.credential_version,
        credential_digest=credential.credential_digest,
        principal_id=credential.principal_id,
        valid_from=credential.valid_from,
        expires_at=credential.expires_at,
        revoked_at=credential.revoked_at,
    )
    session_evidence = OrdinaryAgentSession(
        record_kind="proposed_ordinary_agent_v1",
        authority_state="inert",
        authorizes_execution=False,
        record_id=session.session_id,
        **session.model_dump(exclude={"schema_version", "session_id", "delegation"}),
        session_id=session.session_id,
    )
    lease_evidence = OrdinaryAgentLease(
        record_kind="proposed_ordinary_agent_v1",
        authority_state="inert",
        authorizes_execution=False,
        record_id=lease.lease_id,
        **lease.model_dump(exclude={"schema_version", "revision"}),
    )
    request_evidence = OrdinaryAgentRequest(
        record_kind="proposed_ordinary_agent_v1",
        authority_state="inert",
        authorizes_execution=False,
        record_id=request.request_id,
        request_id=request.request_id,
        idempotency_key=request.idempotency_key,
        principal_id=request.principal_id,
        session_id=request.session_id,
        lease_id=request.lease_id,
        target=request.target,
        base_sha=request.base_sha,
        action="guarded_merge",
        pull_requests=request.pull_requests,
        permitted_stack_edit_pull_requests=request.permitted_stack_edit_pull_requests,
    )
    return credential_evidence, session_evidence, lease_evidence, request_evidence


def require_ordinary_agent_current_job_authority(
    *,
    policy: LaunchplaneAuthzPolicyRecord,
    principal: OrdinaryAgentPrincipalRecord,
    credential: OrdinaryAgentAuthenticationCredentialRecord,
    session: OrdinaryAgentSessionRecord,
    lease: OrdinaryAgentLeaseRecord,
    request: OrdinaryAgentFiniteRequestRecord,
    now: int,
) -> None:
    """Reauthorize an admitted job from storage; never admit or renew caller work.

    Only the internal dispatcher calls this with the persisted original job.
    Its explicit continuation grant replaces interactive timing, never lineage,
    credential validity, revocation, policy, or the original budget window.
    """
    _require_current_credential(principal, credential, now)
    snapshot, subject = _policy_inputs(policy, principal)
    evidence = _eligibility_evidence(
        credential=credential, session=session, lease=lease, request=request
    )
    credential_evidence, session_evidence, lease_evidence, request_evidence = evidence
    reason = ordinary_agent_chain_reason(
        principal=subject,
        credential=credential_evidence,
        session=session_evidence,
        lease=lease_evidence,
        request=request_evidence,
    )
    if reason != "eligible":
        raise OrdinaryAgentSessionAdmissionDenied(reason)
    if request.status != "waiting" or request.cancellation_requested_at is not None:
        raise OrdinaryAgentSessionAdmissionDenied("job_not_dispatchable")
    if now < request.admitted_at or now < session.valid_from or now < lease.valid_from:
        raise OrdinaryAgentSessionAdmissionDenied("job_not_yet_valid")
    if session.revoked_at is not None and session.revoked_at <= now:
        raise OrdinaryAgentSessionAdmissionDenied("session_revoked")
    if lease.revoked_at is not None and lease.revoked_at <= now:
        raise OrdinaryAgentSessionAdmissionDenied("lease_revoked")
    if now >= min(request.expires_at, session.expires_at, lease.expires_at):
        deadline = request.continuation_expires_at
        ceiling = session.delegation.continuation_expires_at
        if (
            deadline is None
            or ceiling is None
            or not now < deadline <= ceiling <= credential.expires_at
        ):
            raise OrdinaryAgentSessionAdmissionDenied("finite_job_expired")
    decision = evaluate_ordinary_agent_policy(
        snapshot=snapshot,
        principal=subject,
        target=lease.target,
        action=lease.action,
        managed_set_id=lease.managed_set_id,
        managed_rule_id=lease.managed_rule_id,
    )
    reason = ordinary_agent_effective_authority_reason(
        policy=decision, lease=lease_evidence, request=request_evidence
    )
    if reason != "eligible":
        raise OrdinaryAgentSessionAdmissionDenied(reason)
    if not lease.budget.window_start <= now < lease.budget.window_end:
        raise OrdinaryAgentSessionAdmissionDenied("budget_window_inactive")
    # Admission already charged the request's PRs; effect reservation spends actions.
    if (
        lease.budget.pull_requests_used > lease.budget.pull_request_limit
        or lease.budget.actions_used > lease.budget.action_limit
    ):
        raise OrdinaryAgentSessionAdmissionDenied("budget_exhausted")


def require_ordinary_agent_finite_job_authority(
    *,
    policy: LaunchplaneAuthzPolicyRecord,
    principal: OrdinaryAgentPrincipalRecord,
    credential: OrdinaryAgentAuthenticationCredentialRecord,
    session: OrdinaryAgentSessionRecord,
    lease: OrdinaryAgentLeaseRecord,
    request: OrdinaryAgentFiniteRequestRecord,
    now: int,
) -> None:
    """Require current job authority and capacity for a new semantic action.

    Already charged effects use current authority plus their stored unique charge;
    they must not acquire a second action merely to finish their first attempt.
    """
    require_ordinary_agent_current_job_authority(
        policy=policy,
        principal=principal,
        credential=credential,
        session=session,
        lease=lease,
        request=request,
        now=now,
    )
    if lease.budget.actions_used >= lease.budget.action_limit:
        raise OrdinaryAgentSessionAdmissionDenied("budget_exhausted")


def rebind_ordinary_agent_finite_request(
    *,
    request: OrdinaryAgentFiniteRequestRecord,
    base_sha: str,
    pull_requests: tuple[OrdinaryAgentPullRequest, ...],
    expected_binding_revision: int,
) -> OrdinaryAgentFiniteRequestRecord:
    """Calculate one bounded refresh after joined current-job authorization.

    Storage performs the revision compare-and-swap in the same transaction.
    Reconciliation-required jobs retain their original binding until resolved.
    """
    if request.status != "waiting" or request.cancellation_requested_at is not None:
        raise OrdinaryAgentSessionAdmissionDenied("job_not_dispatchable")
    if request.binding_revision != expected_binding_revision:
        raise OrdinaryAgentSessionAdmissionDenied("binding_revision_conflict")
    if tuple(item.number for item in pull_requests) != tuple(
        item.number for item in request.pull_requests
    ):
        raise OrdinaryAgentSessionAdmissionDenied("refresh_scope_changed")
    if base_sha == request.base_sha and pull_requests == request.pull_requests:
        return request
    if request.refresh_used >= request.refresh_allowance_total:
        raise OrdinaryAgentSessionAdmissionDenied("refresh_allowance_exhausted")
    return OrdinaryAgentFiniteRequestRecord.model_validate(
        {
            **request.model_dump(),
            "base_sha": base_sha,
            "pull_requests": pull_requests,
            "binding_revision": request.binding_revision + 1,
            "refresh_used": request.refresh_used + 1,
        }
    )


def cancel_ordinary_agent_finite_request(
    *, request: OrdinaryAgentFiniteRequestRecord, now: int
) -> OrdinaryAgentFiniteRequestRecord:
    """Keep unknown-effect reconciliation durable when cancellation is requested."""
    if (
        request.status in ("cancelled", "completed")
        or request.cancellation_requested_at is not None
    ):
        return request
    return OrdinaryAgentFiniteRequestRecord.model_validate(
        {
            **request.model_dump(),
            "cancellation_requested_at": now,
            "status": "reconciliation_required"
            if request.status == "reconciliation_required"
            else "cancelled",
        }
    )
