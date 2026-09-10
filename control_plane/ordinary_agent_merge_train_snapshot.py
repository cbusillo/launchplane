"""Custody-scoped acquisition of durable ordinary merge-train read evidence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import time
from typing import Protocol, cast

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentControllerFence,
    OrdinaryAgentProviderQuotaKey,
    OrdinaryAgentProviderWaitRecord,
    OrdinaryAgentProviderWaitObservation,
    OrdinaryAgentSnapshotStore,
    OrdinaryAgentSnapshotAttemptRecord,
)
from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentCandidateCheckResult,
    OrdinaryAgentMergeTrainSnapshotResult,
    OrdinaryAgentReadmissionObservation,
)
from control_plane.github_app_identity import GitHubApiRequest
from control_plane.ordinary_agent_custody import (
    OrdinaryAgentCustodyAttemptStore,
    OrdinaryAgentCustodySecretStore,
    ordinary_agent_provider_token_lease,
)
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    ORDINARY_CANDIDATE_CHECK_WORK_SECONDS,
    ORDINARY_SNAPSHOT_WORK_SECONDS,
    OrdinaryAgentProviderEvidenceError,
    require_installation_provider_ready,
)
from control_plane.workflows.launchplane import github_api_request
from control_plane.ordinary_agent_read_transport import (
    OrdinaryAgentReadApiTransport,
    ordinary_agent_read_failure_reason,
    ordinary_agent_read_request_counts,
)
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied


SnapshotReader = Callable[
    [DeadlineMergeTrainGitHubTransport],
    OrdinaryAgentMergeTrainSnapshotResult | OrdinaryAgentReadmissionObservation,
]
CandidateCheckReader = Callable[
    [DeadlineMergeTrainGitHubTransport], OrdinaryAgentCandidateCheckResult
]


class OrdinaryAgentReadmissionRequired(RuntimeError):
    """Persisted drift evidence, raised only after its private custody is settled."""

    def __init__(self, *, attempt_id: str, observation_sha256: str) -> None:
        super().__init__("ordinary source readmission required")
        self.attempt_id = attempt_id
        self.observation_sha256 = observation_sha256


class _ProviderWaitStore(Protocol):
    def record_provider_wait(
        self,
        *,
        quota_key: OrdinaryAgentProviderQuotaKey,
        observation: OrdinaryAgentProviderWaitObservation,
    ) -> OrdinaryAgentProviderWaitRecord: ...

    def read_provider_wait(
        self, *, quota_key: OrdinaryAgentProviderQuotaKey
    ) -> OrdinaryAgentProviderWaitRecord | None: ...


def acquire_ordinary_agent_merge_train_snapshot(
    *,
    store: OrdinaryAgentSnapshotStore,
    custody_store: OrdinaryAgentCustodyAttemptStore,
    secret_store: OrdinaryAgentCustodySecretStore,
    request_id: str,
    expected_binding_revision: int,
    controller_fence: OrdinaryAgentControllerFence,
    reader: SnapshotReader,
    api_request: GitHubApiRequest = github_api_request,
    monotonic: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> OrdinaryAgentMergeTrainSnapshotResult:
    attempt = store.reserve_ordinary_agent_snapshot_attempt(
        request_id=request_id,
        expected_binding_revision=expected_binding_revision,
        controller_fence=controller_fence,
    )
    if isinstance(attempt.result, OrdinaryAgentMergeTrainSnapshotResult):
        return attempt.result
    if isinstance(attempt.result, OrdinaryAgentReadmissionObservation):
        raise OrdinaryAgentReadmissionRequired(
            attempt_id=attempt.attempt_id,
            observation_sha256=attempt.result.observation_sha256,
        )
    result = _acquire_read(
        store=store,
        custody_store=custody_store,
        secret_store=secret_store,
        attempt_id=attempt.attempt_id,
        attempt_revision=attempt.revision,
        purpose="snapshot",
        work_seconds=ORDINARY_SNAPSHOT_WORK_SECONDS,
        reader=reader,
        api_request=api_request,
        monotonic=monotonic,
        utc_now=utc_now,
    )
    if isinstance(result, OrdinaryAgentReadmissionObservation):
        raise OrdinaryAgentReadmissionRequired(
            attempt_id=attempt.attempt_id, observation_sha256=result.observation_sha256
        )
    if not isinstance(result, OrdinaryAgentMergeTrainSnapshotResult):
        raise RuntimeError("snapshot reader returned candidate-check evidence")
    return result


def acquire_ordinary_agent_candidate_check(
    *,
    store: OrdinaryAgentSnapshotStore,
    custody_store: OrdinaryAgentCustodyAttemptStore,
    secret_store: OrdinaryAgentCustodySecretStore,
    request_id: str,
    expected_binding_revision: int,
    controller_fence: OrdinaryAgentControllerFence,
    candidate_sha: str,
    reader: CandidateCheckReader,
    api_request: GitHubApiRequest = github_api_request,
    monotonic: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> OrdinaryAgentCandidateCheckResult:
    attempt = store.reserve_ordinary_agent_candidate_check_attempt(
        request_id=request_id,
        expected_binding_revision=expected_binding_revision,
        controller_fence=controller_fence,
        candidate_sha=candidate_sha,
    )
    if isinstance(attempt.result, OrdinaryAgentCandidateCheckResult):
        return attempt.result
    result = _acquire_read(
        store=store,
        custody_store=custody_store,
        secret_store=secret_store,
        attempt_id=attempt.attempt_id,
        attempt_revision=attempt.revision,
        purpose="candidate_check",
        work_seconds=ORDINARY_CANDIDATE_CHECK_WORK_SECONDS,
        reader=reader,
        api_request=api_request,
        monotonic=monotonic,
        utc_now=utc_now,
    )
    if not isinstance(result, OrdinaryAgentCandidateCheckResult):
        raise RuntimeError("candidate-check reader returned snapshot evidence")
    return result


def _acquire_read(
    *,
    store: OrdinaryAgentSnapshotStore,
    custody_store: OrdinaryAgentCustodyAttemptStore,
    secret_store: OrdinaryAgentCustodySecretStore,
    attempt_id: str,
    attempt_revision: int,
    purpose: str,
    work_seconds: int,
    reader: SnapshotReader | CandidateCheckReader,
    api_request: GitHubApiRequest,
    monotonic: Callable[[], float],
    utc_now: Callable[[], datetime],
) -> (
    OrdinaryAgentMergeTrainSnapshotResult
    | OrdinaryAgentCandidateCheckResult
    | OrdinaryAgentReadmissionObservation
):
    reservation = store.reserve_ordinary_agent_read_custody_attempt(
        attempt_id=attempt_id,
        expected_attempt_revision=attempt_revision,
    )
    if (
        reservation.purpose != purpose
        or reservation.candidate.effect_profile != "merge_train_snapshot"
    ):
        raise OrdinaryAgentProviderEvidenceError("read_custody_profile_mismatch")
    started = monotonic()
    transport: DeadlineMergeTrainGitHubTransport | None = None
    result: (
        OrdinaryAgentMergeTrainSnapshotResult
        | OrdinaryAgentCandidateCheckResult
        | OrdinaryAgentReadmissionObservation
        | None
    ) = None
    recorded: OrdinaryAgentSnapshotAttemptRecord | None = None
    try:
        with ordinary_agent_provider_token_lease(
            record_store=custody_store,
            secret_store=secret_store,
            candidate=reservation.candidate,
            idempotency_key=reservation.idempotency_key,
            request_payload=reservation.request_payload,
            api_request=api_request,
            monotonic=monotonic,
            utc_now=utc_now,
            quota_writer=cast(_ProviderWaitStore, store).record_provider_wait,
            before_token_mint=lambda app_id, installation_id: require_installation_provider_ready(
                app_id=app_id,
                installation_id=installation_id,
                resource_classes=("core", "graphql", "secondary"),
                read_provider_wait=cast(_ProviderWaitStore, store).read_provider_wait,
                utc_now=utc_now,
            ),
        ) as lease:
            token_expiry = datetime.fromisoformat(
                lease.installation_token.expires_at.replace("Z", "+00:00")
            )
            token_seconds = (token_expiry - utc_now().astimezone(timezone.utc)).total_seconds()
            transport = DeadlineMergeTrainGitHubTransport(
                transport=OrdinaryAgentReadApiTransport(
                    token=lease.installation_token.token,
                    api_request=api_request,
                    installation_id=lease.installation_token.installation_id,
                    store=cast(_ProviderWaitStore, store),
                    utc_now=utc_now,
                ),
                work_deadline=started + work_seconds,
                token_deadline=monotonic() + token_seconds,
                monotonic=monotonic,
            )
            result = reader(transport)
            if result.counts != ordinary_agent_read_request_counts(transport):
                raise OrdinaryAgentProviderEvidenceError("provider_request_counts_mismatch")
            if purpose == "snapshot" and isinstance(
                result, (OrdinaryAgentMergeTrainSnapshotResult, OrdinaryAgentReadmissionObservation)
            ):
                recorded = store.record_ordinary_agent_snapshot_success(
                    attempt_id=attempt_id,
                    custody_attempt_id=reservation.custody_attempt_id,
                    result=result,
                )
            elif purpose == "candidate_check" and isinstance(
                result, OrdinaryAgentCandidateCheckResult
            ):
                recorded = store.record_ordinary_agent_candidate_check_success(
                    attempt_id=attempt_id,
                    custody_attempt_id=reservation.custody_attempt_id,
                    result=result,
                )
            else:
                raise OrdinaryAgentProviderEvidenceError("provider_result_type_mismatch")
    except Exception as error:
        counts = ordinary_agent_read_request_counts(transport)
        reason = ordinary_agent_read_failure_reason(error)
        store.record_ordinary_agent_read_failure(
            attempt_id=attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            reason_code=reason,
            counts=counts,
        )
        raise
    # The response is immutable; report its decision only after custody cleanup,
    # outside the provider-failure handler so it cannot append a conflicting outcome.
    if recorded is not None and recorded.state in {"fenced", "exhausted"}:
        raise OrdinaryAgentSessionAdmissionDenied(recorded.reason_code or "read_attempts_exhausted")
    if (
        isinstance(result, OrdinaryAgentMergeTrainSnapshotResult)
        and recorded is not None
        and recorded.reason_code == "source_checks_undecided"
    ):
        raise OrdinaryAgentSessionAdmissionDenied("source_check_wait")
    if result is None:
        raise RuntimeError("ordinary provider reader returned no result")
    if isinstance(
        result, (OrdinaryAgentMergeTrainSnapshotResult, OrdinaryAgentReadmissionObservation)
    ):
        return result
    if isinstance(result, OrdinaryAgentCandidateCheckResult):
        return result
    raise RuntimeError("ordinary provider reader returned the wrong result type")
