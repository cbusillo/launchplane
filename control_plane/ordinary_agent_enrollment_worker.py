"""Recover approved enrollment directly from its authoritative domain operation."""

from dataclasses import dataclass
from typing import Protocol, cast

from control_plane.contracts.ordinary_agent_lifecycle import (
    ORDINARY_AGENT_ENROLLMENT_MUTATION_ROUTE,
    ORDINARY_AGENT_ENROLLMENT_MUTATION_SCOPE,
    OrdinaryAgentEnrollmentApprovalReference,
    ordinary_agent_enrollment_envelope_sha256,
)
from control_plane.durable_operation_authorization import read_active_authz_policy_record
from control_plane.ordinary_agent_authentication import issue_ordinary_agent_credential
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore


class _EnrollmentDiscovery(Protocol):
    def list_pending_approved_ordinary_agent_enrollments(
        self, *, limit: int, after: OrdinaryAgentEnrollmentApprovalReference | None
    ) -> tuple[OrdinaryAgentEnrollmentApprovalReference, ...]: ...


@dataclass
class OrdinaryAgentEnrollmentRecoveryState:
    after: OrdinaryAgentEnrollmentApprovalReference | None = None


@dataclass(frozen=True)
class OrdinaryAgentEnrollmentRecoveryResult:
    processed: int = 0
    applied: int = 0
    blocked: int = 0
    failed: int = 0


def recover_ordinary_agent_enrollments_once(
    *,
    record_store: object,
    state: OrdinaryAgentEnrollmentRecoveryState,
    lease_owner: str,
    limit: int,
) -> OrdinaryAgentEnrollmentRecoveryResult:
    """Isolate per-operation failures and never return secret material or exception detail."""
    processed = applied = blocked = failed = 0
    try:
        if not isinstance(record_store, PostgresRecordStore) or not 1 <= limit <= 100:
            raise ValueError("enrollment recovery requires bounded shared storage")
        references = cast(
            _EnrollmentDiscovery, record_store
        ).list_pending_approved_ordinary_agent_enrollments(limit=limit, after=state.after)
    except Exception:
        return OrdinaryAgentEnrollmentRecoveryResult(failed=1)
    if not references:
        state.after = None
    for reference in references:
        state.after = reference
        processed += 1
        try:
            approved = record_store.read_approved_ordinary_agent_enrollment(
                principal_id=reference.principal_id, operation_id=reference.operation_id
            )
            intent = approved.intent
            policy = read_active_authz_policy_record(record_store)
            # Approval already proved its immutable administrator rule. Require
            # both exact approved policy snapshots before preparing ciphertext;
            # joined apply repeats authority/current-state checks after this read.
            if (
                policy.record_id != intent.policy.record_id
                or policy.revision != intent.policy.revision
                or policy.policy_sha256 != intent.policy.policy_sha256
                or policy.record_id != approved.administrator.policy_record_id
                or policy.revision != approved.administrator.policy_revision
                or policy.policy_sha256 != approved.administrator.policy_sha256
            ):
                blocked += 1
                continue
            planned = intent.authentication_credential
            issuance = issue_ordinary_agent_credential(
                principal_id=intent.principal_id,
                credential_id=planned.credential_id,
                credential_version=1
                if intent.action == "enroll"
                else (intent.credential_version or 0) + 1,
                valid_from=planned.valid_from,
                expires_at=planned.expires_at,
                operation_id=intent.operation_id,
                receiver_claim_sha256=intent.delivery.receiver_claim_sha256,
                delivery_expires_at=intent.delivery.expires_at,
                intent_sha256=approved.issuance_intent_sha256,
            )
            envelope = approved.apply_envelope(issuance.candidate)
            result = record_store.apply_approved_ordinary_agent_enrollment(
                envelope=envelope,
                issuance=issuance,
                mutation=DbOnlyMutationRequest(
                    scope=ORDINARY_AGENT_ENROLLMENT_MUTATION_SCOPE,
                    route_path=ORDINARY_AGENT_ENROLLMENT_MUTATION_ROUTE,
                    idempotency_key=envelope.operation_id,
                    request_fingerprint=ordinary_agent_enrollment_envelope_sha256(envelope),
                    lease_owner=lease_owner,
                    response_status_code=200,
                    response_trace_id=f"ordinary-enrollment:{envelope.operation_id}",
                    response_payload={},
                ),
            )
            if result.status in {"written", "replayed"}:
                applied += 1
            else:
                blocked += 1
        except Exception:
            failed += 1
    return OrdinaryAgentEnrollmentRecoveryResult(
        processed=processed, applied=applied, blocked=blocked, failed=failed
    )
