"""Dormant controller-free worker step for administrator qualification."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import time
from typing import Literal, Protocol

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentClaimedJob,
    OrdinaryAgentJobAttemptDisposition,
    OrdinaryAgentProviderQuotaKey,
    OrdinaryAgentProviderWaitObservation,
    OrdinaryAgentProviderWaitRecord,
    OrdinaryAgentQualificationAttemptRecord,
    OrdinaryAgentQualificationStore,
)
from control_plane.contracts.ordinary_agent_qualification import (
    OrdinaryAgentQualificationAttestation,
    OrdinaryAgentQualificationSetup,
    qualification_identity,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentQualificationFiniteRequestV2,
)
from control_plane.github_app_identity import GitHubApiRequest
from control_plane.ordinary_agent_custody import (
    OrdinaryAgentCustodyAttemptStore,
    OrdinaryAgentCustodySecretStore,
    ordinary_agent_provider_token_lease,
)
from control_plane.contracts.ordinary_agent_custody import OrdinaryAgentCustodyIssueAttempt
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    require_installation_provider_ready,
)
from control_plane.ordinary_agent_qualification import observe_repository_administrator
from control_plane.ordinary_agent_read_transport import (
    OrdinaryAgentReadApiTransport,
    ordinary_agent_read_failure_reason,
    ordinary_agent_read_request_counts,
)
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.workflows.launchplane import github_api_request


ORDINARY_QUALIFICATION_WORK_SECONDS = 45


class OrdinaryAgentQualificationSetupResolver(Protocol):
    def __call__(
        self, *, request: OrdinaryAgentQualificationFiniteRequestV2
    ) -> OrdinaryAgentQualificationSetup | None: ...


class _QualificationStore(
    OrdinaryAgentQualificationStore,
    OrdinaryAgentCustodyAttemptStore,
    OrdinaryAgentCustodySecretStore,
    Protocol,
):
    def record_provider_wait(
        self,
        *,
        quota_key: OrdinaryAgentProviderQuotaKey,
        observation: OrdinaryAgentProviderWaitObservation,
    ) -> OrdinaryAgentProviderWaitRecord: ...

    def read_provider_wait(
        self, *, quota_key: OrdinaryAgentProviderQuotaKey
    ) -> OrdinaryAgentProviderWaitRecord | None: ...

    def read_ordinary_agent_custody_issue_attempt(
        self, attempt_id: str
    ) -> OrdinaryAgentCustodyIssueAttempt: ...


def advance_ordinary_agent_qualification_job(
    *,
    claimed: OrdinaryAgentClaimedJob,
    store: _QualificationStore,
    setup_resolver: OrdinaryAgentQualificationSetupResolver,
    api_request: GitHubApiRequest = github_api_request,
    monotonic: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> OrdinaryAgentJobAttemptDisposition:
    """Advance one read-only qualification attempt; this function is unregistered."""
    if not isinstance(claimed.request, OrdinaryAgentQualificationFiniteRequestV2):
        return OrdinaryAgentJobAttemptDisposition(
            status="blocked", reason_code="request_purpose_unsupported"
        )
    if claimed.controller_fence is not None:
        return OrdinaryAgentJobAttemptDisposition(
            status="blocked", reason_code="qualification_controller_fence"
        )
    setup = setup_resolver(request=claimed.request)
    if setup is None:
        return OrdinaryAgentJobAttemptDisposition(
            status="blocked", reason_code="qualification_setup_unavailable"
        )
    try:
        attempt = store.reserve_ordinary_agent_qualification_attempt(
            claim_fence=claimed.claim_fence, setup=setup
        )
    except OrdinaryAgentSessionAdmissionDenied as error:
        return _denied_disposition(error)
    if attempt.state == "completed":
        return _completed_disposition(store=store, attempt=attempt)
    if attempt.state in {"fenced", "exhausted"}:
        return OrdinaryAgentJobAttemptDisposition(
            status="reconciliation_required" if attempt.state == "fenced" else "blocked",
            reason_code=attempt.reason_code or "qualification_attempt_closed",
        )
    try:
        reservation = store.reserve_ordinary_agent_qualification_custody_attempt(
            claim_fence=claimed.claim_fence,
            attempt_id=attempt.attempt_id,
            expected_attempt_revision=attempt.revision,
        )
    except OrdinaryAgentSessionAdmissionDenied as error:
        return _denied_disposition(error)
    transport: DeadlineMergeTrainGitHubTransport | None = None
    recorded: OrdinaryAgentQualificationAttemptRecord | None = None
    result_denial: OrdinaryAgentSessionAdmissionDenied | None = None
    read_authority_denied = False
    readiness_denial: OrdinaryAgentSessionAdmissionDenied | None = None

    def require_pre_mint_readiness(app_id: int, installation_id: int) -> None:
        nonlocal readiness_denial
        try:
            store.require_ordinary_agent_qualification_runtime_readiness(
                claim_fence=claimed.claim_fence,
                attempt_id=attempt.attempt_id,
            )
        except OrdinaryAgentSessionAdmissionDenied as error:
            readiness_denial = error
            raise
        require_installation_provider_ready(
            app_id=app_id,
            installation_id=installation_id,
            resource_classes=("core", "graphql", "secondary"),
            read_provider_wait=store.read_provider_wait,
            utc_now=utc_now,
        )

    try:
        started = monotonic()
        with ordinary_agent_provider_token_lease(
            record_store=store,
            secret_store=store,
            candidate=reservation.candidate,
            idempotency_key=reservation.idempotency_key,
            request_payload=reservation.request_payload,
            api_request=api_request,
            monotonic=monotonic,
            utc_now=utc_now,
            quota_writer=store.record_provider_wait,
            before_token_mint=require_pre_mint_readiness,
        ) as lease:
            expires_at = datetime.fromisoformat(
                lease.installation_token.expires_at.replace("Z", "+00:00")
            )
            token_seconds = (expires_at - utc_now().astimezone(timezone.utc)).total_seconds()
            transport = DeadlineMergeTrainGitHubTransport(
                transport=OrdinaryAgentReadApiTransport(
                    token=lease.installation_token.token,
                    api_request=api_request,
                    installation_id=lease.installation_token.installation_id,
                    store=store,
                    utc_now=utc_now,
                ),
                work_deadline=started + ORDINARY_QUALIFICATION_WORK_SECONDS,
                token_deadline=monotonic() + token_seconds,
                monotonic=monotonic,
            )
            try:
                store.require_ordinary_agent_qualification_runtime_readiness(
                    claim_fence=claimed.claim_fence,
                    attempt_id=attempt.attempt_id,
                )
                store.require_ordinary_agent_qualification_read_authority(
                    claim_fence=claimed.claim_fence, attempt_id=attempt.attempt_id
                )
            except OrdinaryAgentSessionAdmissionDenied as error:
                if error.reason_code != "qualification_read_authority_lost":
                    readiness_denial = error
                read_authority_denied = True
                raise
            observed = observe_repository_administrator(
                transport=transport, setup=setup, observed_at=int(utc_now().timestamp())
            )
            attestation = _attestation(
                attempt=attempt,
                custody_attempt_id=reservation.custody_attempt_id,
                observed=observed,
            )
            try:
                recorded = store.record_ordinary_agent_qualification_result(
                    attempt_id=attempt.attempt_id,
                    custody_attempt_id=reservation.custody_attempt_id,
                    result=observed,
                    attestation=attestation,
                )
            except OrdinaryAgentSessionAdmissionDenied as error:
                result_denial = error
    except Exception as error:
        if readiness_denial is not None:
            return OrdinaryAgentJobAttemptDisposition(
                status="blocked", reason_code=readiness_denial.reason_code
            )
        reason = _failure_reason(error)
        try:
            recorded = store.record_ordinary_agent_qualification_failure(
                attempt_id=attempt.attempt_id,
                custody_attempt_id=reservation.custody_attempt_id,
                reason_code=reason,
                counts=ordinary_agent_read_request_counts(transport),
            )
        except OrdinaryAgentSessionAdmissionDenied:
            return OrdinaryAgentJobAttemptDisposition(
                status="reconciliation_required", reason_code="qualification_history_conflict"
            )
        if reason == "cleanup_unknown":
            return OrdinaryAgentJobAttemptDisposition(
                status="reconciliation_required", reason_code=reason
            )
        if read_authority_denied:
            return OrdinaryAgentJobAttemptDisposition(
                status="blocked", reason_code="qualification_read_authority_lost"
            )
        return OrdinaryAgentJobAttemptDisposition(
            status="waiting", next_due_at=recorded.next_due_at, reason_code=recorded.reason_code
        )
    if result_denial is not None:
        return OrdinaryAgentJobAttemptDisposition(
            status="reconciliation_required", reason_code=result_denial.reason_code
        )
    if recorded is None:
        return OrdinaryAgentJobAttemptDisposition(
            status="reconciliation_required", reason_code="qualification_outcome_missing"
        )
    return _completed_disposition(store=store, attempt=recorded)


def _attestation(
    *,
    attempt: OrdinaryAgentQualificationAttemptRecord,
    custody_attempt_id: str,
    observed: object,
) -> OrdinaryAgentQualificationAttestation | None:
    from control_plane.contracts.ordinary_agent_qualification import (
        OrdinaryRepositoryAdminObservation,
    )

    if (
        not isinstance(observed, OrdinaryRepositoryAdminObservation)
        or observed.status != "qualified"
    ):
        return None
    body: dict[str, object] = {
        "schema_version": 1,
        "request_id": attempt.request_id,
        "scope_sha256": attempt.scope_sha256,
        "binding_revision": attempt.binding_revision,
        "target": attempt.setup.target.model_dump(mode="json"),
        "lease_action": "preflight",
        "source_activation_operation_id": attempt.setup.source_activation_operation_id,
        "source_activation_binding_sha256": attempt.setup.source_activation_binding_sha256,
        "administrator": qualification_identity(
            github_id=attempt.setup.administrator_github_id, login=attempt.setup.administrator_login
        ).model_dump(mode="json"),
        "principal_id": attempt.principal_id,
        "credential_id": attempt.credential_id,
        "credential_version": attempt.credential_version,
        "policy_managed_set_id": attempt.setup.managed_set_id,
        "policy_managed_rule_id": attempt.setup.managed_rule_id,
        "custody_record_id": attempt.custody_record_id,
        "custody_sha256": attempt.custody_sha256,
        "repository_inventory_record_id": attempt.repository_inventory_record_id,
        "repository_inventory_revision": attempt.repository_inventory_revision,
        "repository_inventory_digest": attempt.repository_inventory_digest,
        "github_app_id": attempt.github_app_id,
        "github_installation_id": attempt.github_installation_id,
        "managed_secret_binding_id": attempt.managed_secret_binding_id,
        "managed_secret_id": attempt.managed_secret_id,
        "managed_secret_version_id": attempt.managed_secret_version_id,
        "provider_inspection_sha256": attempt.provider_inspection_sha256,
        "installed_permission_ceiling_sha256": attempt.installed_permission_ceiling_sha256,
        "effect_profile": "merge_train_snapshot",
        "read_profile_sha256": attempt.read_profile_sha256,
        "custody_attempt_id": custody_attempt_id,
        "observation": observed.model_dump(mode="json"),
        "expires_at": attempt.setup.attestation_expires_at,
    }
    return OrdinaryAgentQualificationAttestation.model_validate(
        {**body, "attestation_sha256": canonical_json_sha256(body)}
    )


def _completed_disposition(
    *, store: _QualificationStore, attempt: OrdinaryAgentQualificationAttemptRecord
) -> OrdinaryAgentJobAttemptDisposition:
    if attempt.state in {"fenced", "exhausted"}:
        return OrdinaryAgentJobAttemptDisposition(
            status="reconciliation_required" if attempt.state == "fenced" else "blocked",
            reason_code=attempt.reason_code or "qualification_attempt_closed",
        )
    if not attempt.custody_attempt_ids:
        return OrdinaryAgentJobAttemptDisposition(
            status="reconciliation_required", reason_code="read_custody_fenced"
        )
    custody = store.read_ordinary_agent_custody_issue_attempt(attempt.custody_attempt_ids[-1])
    if custody.state != "closed":
        return OrdinaryAgentJobAttemptDisposition(
            status="reconciliation_required", reason_code="read_custody_fenced"
        )
    if attempt.result is None:
        return OrdinaryAgentJobAttemptDisposition(
            status="reconciliation_required", reason_code="qualification_outcome_missing"
        )
    if attempt.result.status == "qualified" and attempt.attestation is not None:
        return OrdinaryAgentJobAttemptDisposition(
            status="completed", reason_code="qualification_attested"
        )
    return OrdinaryAgentJobAttemptDisposition(status="blocked", reason_code=attempt.result.status)


def _failure_reason(
    error: Exception,
) -> Literal[
    "provider_wait",
    "provider_attempt_deadline",
    "provider_incomplete",
    "provider_transport",
    "cleanup_unknown",
]:
    reason = ordinary_agent_read_failure_reason(error)
    if reason == "snapshot_query_cost_exceeded":
        return "provider_incomplete"
    return reason


def _denied_disposition(
    error: OrdinaryAgentSessionAdmissionDenied,
) -> OrdinaryAgentJobAttemptDisposition:
    if error.retry_not_before is not None:
        return OrdinaryAgentJobAttemptDisposition(
            status="waiting", next_due_at=error.retry_not_before, reason_code=error.reason_code
        )
    return OrdinaryAgentJobAttemptDisposition(status="blocked", reason_code=error.reason_code)
