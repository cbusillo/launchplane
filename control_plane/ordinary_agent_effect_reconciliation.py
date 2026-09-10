"""Observe one uncertain effect through supported custody without resending it."""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Protocol
import time

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
)
from control_plane.github_app_identity import GitHubApiRequest
from control_plane.ordinary_agent_quota_transport import OrdinaryAgentQuotaTransport
from control_plane.merge_train_github import (
    MergeTrainGitHubTransport,
    MergeTrainGitHubError,
)
from control_plane.ordinary_agent_custody import (
    OrdinaryAgentCustodyAttemptStore,
    OrdinaryAgentCustodySecretStore,
    ordinary_agent_provider_token_lease,
)
from control_plane.ordinary_agent_effect_recovery import recover_ordinary_effect
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    ORDINARY_CANDIDATE_CHECK_WORK_SECONDS,
    OrdinaryAgentProviderDeferred,
    OrdinaryAgentProviderEvidenceError,
    require_installation_provider_ready,
)
from control_plane.ordinary_agent_reconciliation_reader import read_ordinary_effect_observation
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.workflows.launchplane import github_api_request


class OrdinaryEffectReconciliationStore(
    effects.OrdinaryAgentEffectStore,
    OrdinaryAgentCustodyAttemptStore,
    OrdinaryAgentCustodySecretStore,
    Protocol,
):
    pass


class _ReadOnlyTransport:
    def __init__(self, transport: MergeTrainGitHubTransport) -> None:
        self._transport = transport

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        if method != "GET" or body is not None:
            raise OrdinaryAgentSessionAdmissionDenied("reconciliation_read_only")
        return self._transport.request(method=method, path=path)


def reconcile_ordinary_effect_once(
    *,
    store: OrdinaryEffectReconciliationStore,
    request: OrdinaryAgentFiniteRequestRecord,
    effect_id: str,
    api_request: GitHubApiRequest = github_api_request,
    transport_factory: Callable[[str], MergeTrainGitHubTransport] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> effects.OrdinaryAgentEffectHistory:
    history = store.read_ordinary_agent_effect_history(effect_id=effect_id)
    record, child = history.effect, history.child
    if (
        record.request_id != request.request_id
        or record.binding_revision != request.binding_revision
        or record.scope_sha256 != request.scope_sha256
        or record.target != request.target
    ):
        raise OrdinaryAgentSessionAdmissionDenied("reconciliation_request_conflict")
    if recover_ordinary_effect(history).disposition != "observe":
        return history
    if child is None:
        raise OrdinaryAgentSessionAdmissionDenied("reconciliation_provenance_unavailable")
    reservation = store.reserve_ordinary_reconciliation_custody_attempt(
        effect_id=effect_id,
        expected_effect_revision=record.revision,
    )
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
        before_token_mint=lambda app_id, installation_id: require_installation_provider_ready(
            app_id=app_id,
            installation_id=installation_id,
            resource_classes=("core", "secondary"),
            read_provider_wait=store.read_provider_wait,
            utc_now=utc_now,
        ),
    ) as lease:
        anchor, epoch = monotonic(), utc_now().timestamp()
        expiry = datetime.fromisoformat(
            lease.installation_token.expires_at.replace("Z", "+00:00")
        ).timestamp()
        transport = DeadlineMergeTrainGitHubTransport(
            transport=_ReadOnlyTransport(
                OrdinaryAgentQuotaTransport(
                    token=lease.installation_token.token,
                    installation_id=lease.installation_token.installation_id,
                    writer=store.record_provider_wait,
                    transport=(
                        transport_factory(lease.installation_token.token)
                        if transport_factory is not None
                        else None
                    ),
                    utc_now=utc_now,
                )
            ),
            work_deadline=started + ORDINARY_CANDIDATE_CHECK_WORK_SECONDS,
            token_deadline=anchor + max(0, expiry - epoch),
            monotonic=monotonic,
        )
        try:
            observation = read_ordinary_effect_observation(transport, record)
        except OrdinaryAgentProviderDeferred as error:
            observation = effects.OrdinaryAgentIncompleteReadObservation(
                repository=record.target.repository,
                reason="provider_wait"
                if error.reason_code == "provider_wait"
                else "provider_attempt_deadline",
            )
        except OrdinaryAgentProviderEvidenceError:
            observation = effects.OrdinaryAgentIncompleteReadObservation(
                repository=record.target.repository,
                reason="provider_incomplete",
            )
        except MergeTrainGitHubError:
            observation = effects.OrdinaryAgentIncompleteReadObservation(
                repository=record.target.repository,
                reason="provider_transport",
            )
        store.append_ordinary_effect_reconciliation(
            child_id=child.child_id,
            typed_observation=effects.OrdinaryAgentReconciliationObservation(
                observation_id="observation-" + reservation.attempt_id,
                custody_attempt_id=reservation.attempt_id,
                observed_at=int(utc_now().timestamp()),
                observation=observation,
            ),
        )
    # Cleanup may fail after the durable observation. That error is preserved;
    # a later invocation reads history before deciding whether any lease is needed.
    return store.read_ordinary_agent_effect_history(effect_id=effect_id)
