"""Custody-scoped acquisition of durable ordinary merge-train read evidence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import time

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentControllerFence,
    OrdinaryAgentSnapshotStore,
)
from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentCandidateCheckResult,
    OrdinaryAgentMergeTrainSnapshotResult,
    OrdinaryAgentProviderRequestCounts,
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
)
from control_plane.workflows.launchplane import github_api_request


SnapshotReader = Callable[
    [DeadlineMergeTrainGitHubTransport], OrdinaryAgentMergeTrainSnapshotResult
]
CandidateCheckReader = Callable[
    [DeadlineMergeTrainGitHubTransport], OrdinaryAgentCandidateCheckResult
]


class _ApiRequestTransport:
    def __init__(self, *, token: str, api_request: GitHubApiRequest) -> None:
        self._token = token
        self._api_request = api_request

    def request(
        self, *, method: str, path: str, body: dict[str, object] | None = None
    ) -> object:
        values: dict[str, object] = {
            "method": method,
            "path": path,
            "token": self._token,
        }
        if body is not None:
            values["body"] = body
        return self._api_request(**values)


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
    return _acquire_read(
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
    return _acquire_read(
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
) -> OrdinaryAgentMergeTrainSnapshotResult | OrdinaryAgentCandidateCheckResult:
    reservation = store.reserve_ordinary_agent_read_custody_attempt(
        attempt_id=attempt_id,
        expected_attempt_revision=attempt_revision,
    )
    started = monotonic()
    transport: DeadlineMergeTrainGitHubTransport | None = None
    result: OrdinaryAgentMergeTrainSnapshotResult | OrdinaryAgentCandidateCheckResult | None = None
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
        ) as lease:
            token_expiry = datetime.fromisoformat(
                lease.installation_token.expires_at.replace("Z", "+00:00")
            )
            token_seconds = (token_expiry - utc_now().astimezone(timezone.utc)).total_seconds()
            transport = DeadlineMergeTrainGitHubTransport(
                transport=_ApiRequestTransport(
                    token=lease.installation_token.token,
                    api_request=api_request,
                ),
                work_deadline=started + work_seconds,
                token_deadline=monotonic() + token_seconds,
                monotonic=monotonic,
            )
            result = reader(transport)  # type: ignore[arg-type]
    except Exception as error:
        counts = _request_counts(transport)
        reason = (
            error.reason_code
            if isinstance(error, OrdinaryAgentProviderEvidenceError)
            else "provider_transport"
        )
        if reason not in {
            "provider_wait",
            "provider_incomplete",
            "provider_transport",
            "snapshot_query_cost_exceeded",
            "cleanup_unknown",
        }:
            reason = "provider_incomplete"
        store.record_ordinary_agent_read_failure(
            attempt_id=attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            reason_code=reason,  # type: ignore[arg-type]
            counts=counts,
        )
        raise
    if result is None:
        raise RuntimeError("ordinary provider reader returned no result")
    if result.counts != _request_counts(transport):
        store.record_ordinary_agent_read_failure(
            attempt_id=attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            reason_code="provider_incomplete",
            counts=_request_counts(transport),
        )
        raise OrdinaryAgentProviderEvidenceError("provider_request_counts_mismatch")
    if purpose == "snapshot" and isinstance(result, OrdinaryAgentMergeTrainSnapshotResult):
        store.record_ordinary_agent_snapshot_success(
            attempt_id=attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            result=result,
        )
        return result
    if purpose == "candidate_check" and isinstance(result, OrdinaryAgentCandidateCheckResult):
        store.record_ordinary_agent_candidate_check_success(
            attempt_id=attempt_id,
            custody_attempt_id=reservation.custody_attempt_id,
            result=result,
        )
        return result
    raise RuntimeError("ordinary provider reader returned the wrong result type")


def _request_counts(
    transport: DeadlineMergeTrainGitHubTransport | None,
) -> OrdinaryAgentProviderRequestCounts:
    return OrdinaryAgentProviderRequestCounts(
        rest_core_requests=transport.rest_core_requests if transport else 0,
        graphql_requests=transport.graphql_requests if transport else 0,
        graphql_points=transport.graphql_points if transport else 0,
    )
