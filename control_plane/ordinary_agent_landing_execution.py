"""Join one fresh landing preparation, provider lease, admission and dispatch."""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal, Protocol
import time

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentControllerFence,
    OrdinaryAgentEffectStore,
    OrdinaryAgentLandingStore,
    OrdinaryAgentLandingPreparation,
)
from control_plane.contracts.ordinary_agent_snapshot import OrdinaryAgentLandingEvidence
from control_plane.github_app_identity import GitHubApiRequest
from control_plane.ordinary_agent_quota_transport import OrdinaryAgentQuotaTransport
from control_plane.merge_admission import GuardedMergeAdmission, MergeAdmissionDeniedError
from control_plane.merge_train_github import (
    MergeTrainGitHubTransport,
)
from control_plane.ordinary_agent_custody import (
    OrdinaryAgentCustodyAttemptStore,
    OrdinaryAgentCustodySecretStore,
    ordinary_agent_provider_token_lease,
)
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderDeferred,
    OrdinaryAgentProviderEvidenceError,
    ORDINARY_MUTATION_WORK_SECONDS,
    require_installation_provider_ready,
)
from control_plane.ordinary_agent_landing_dispatch import FinalizedOrdinaryLandingDispatcher
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.ordinary_agent_landing_reader import read_ordinary_agent_landing_evidence
from control_plane.workflows.launchplane import github_api_request


class OrdinaryAgentLandingExecutionStore(
    OrdinaryAgentLandingStore,
    OrdinaryAgentEffectStore,
    OrdinaryAgentCustodyAttemptStore,
    OrdinaryAgentCustodySecretStore,
    Protocol,
):
    pass


class OrdinaryLandingRecoveryRequired(RuntimeError):
    """An existing reservation must be recovered from history, never reminted."""

    def __init__(self, preparation_id: str):
        self.preparation_id = preparation_id
        super().__init__("Existing landing preparation requires read-only recovery")


def execute_fresh_ordinary_landing(
    *,
    store: OrdinaryAgentLandingExecutionStore,
    request_id: str,
    binding_revision: int,
    controller_fence: OrdinaryAgentControllerFence,
    pull_request_number: int,
    semantic_ordinal: int,
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_plan_record: MergeTrainBatchLandingPlanRecord,
    repository_owner_id: int,
    repository_policy: MergeTrainRepositoryPolicy,
    guard_factory: Callable[
        [OrdinaryAgentLandingPreparation, OrdinaryAgentLandingEvidence], GuardedMergeAdmission
    ],
    checkpoint: Callable[[MergeTrainBatchLandingEntry], None],
    api_request: GitHubApiRequest = github_api_request,
    transport_factory: Callable[[str], MergeTrainGitHubTransport] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> MergeTrainBatchLandingEntry:
    reservation = store.reserve_ordinary_landing_preparation(
        request_id=request_id,
        expected_binding_revision=binding_revision,
        controller_fence=controller_fence,
        pull_request_number=pull_request_number,
        semantic_ordinal=semantic_ordinal,
    )
    if reservation.disposition != "created":
        raise OrdinaryLandingRecoveryRequired(reservation.preparation.preparation_id)
    preparation = reservation.preparation
    try:
        with ordinary_agent_provider_token_lease(
            record_store=store,
            secret_store=store,
            candidate=preparation.candidate,
            idempotency_key=preparation.idempotency_key,
            request_payload=preparation.request_payload,
            api_request=api_request,
            monotonic=monotonic,
            utc_now=utc_now,
            quota_writer=store.record_provider_wait,
            before_token_mint=lambda app_id, installation_id: require_installation_provider_ready(
                app_id=app_id,
                installation_id=installation_id,
                resource_classes=("core", "graphql", "secondary"),
                read_provider_wait=store.read_provider_wait,
                utc_now=utc_now,
            ),
        ) as lease:
            preparation = store.read_ordinary_landing_preparation(
                preparation_id=preparation.preparation_id
            )
            anchor = monotonic()
            now = utc_now().timestamp()
            if preparation.work_expires_at is None:
                raise RuntimeError(
                    "Landing custody issuance did not stamp its preparation deadline"
                )
            transport = DeadlineMergeTrainGitHubTransport(
                transport=OrdinaryAgentQuotaTransport(
                    token=lease.installation_token.token,
                    installation_id=lease.installation_token.installation_id,
                    writer=store.record_provider_wait,
                    transport=(
                        transport_factory(lease.installation_token.token)
                        if transport_factory is not None
                        else None
                    ),
                    utc_now=utc_now,
                ),
                work_deadline=anchor
                + min(ORDINARY_MUTATION_WORK_SECONDS, max(0, preparation.work_expires_at - now)),
                token_deadline=anchor
                + max(
                    0,
                    datetime.fromisoformat(
                        lease.installation_token.expires_at.replace("Z", "+00:00")
                    ).timestamp()
                    - now,
                ),
                monotonic=monotonic,
            )
            evidence = read_ordinary_agent_landing_evidence(
                transport=transport,
                preparation=preparation,
                candidate_record=candidate_record,
                landing_plan_record=landing_plan_record,
                repository_owner_id=repository_owner_id,
                repository_policy=repository_policy,
                utc_seconds=lambda: utc_now().timestamp(),
            )
            preparation = store.record_ordinary_landing_evidence(
                preparation_id=preparation.preparation_id,
                expected_revision=preparation.revision,
                controller_fence=controller_fence,
                evidence=evidence,
            )
            guard = guard_factory(preparation, evidence)
            proposal = guard.build_proposal(
                entry=preparation.entry,
                observed_base_sha=evidence.base_identity.sha,
                observed_base_tree_sha=evidence.base_identity.tree_sha,
                observed_head_sha=evidence.repository_evidence.target.head_sha,
                observed_head_tree_sha=evidence.repository_evidence.target.tree_sha,
            )
            finalization = store.finalize_ordinary_landing_preparation(
                preparation_id=preparation.preparation_id,
                expected_revision=preparation.revision,
                controller_fence=controller_fence,
                proposal=proposal,
                custody_attempt_id=lease.attempt_id,
            )
            if finalization.disposition != "created":
                raise OrdinaryLandingRecoveryRequired(preparation.preparation_id)
            preparation = finalization.preparation
            result_sha = FinalizedOrdinaryLandingDispatcher(
                finalization=finalization,
                transport=transport,
                store=store,
                utc_seconds=lambda: utc_now().timestamp(),
            ).dispatch()
            entry = preparation.entry.model_copy(
                update={
                    "status": "merged",
                    "landed_head_sha": preparation.entry.expected_head_sha,
                    "landed_head_tree_sha": preparation.entry.expected_head_tree_sha,
                    "merge_commit_sha": result_sha,
                    "merge_commit_tree_sha": preparation.expected_merge_tree_sha,
                    "recorded_rolling_base_sha": preparation.expected_base_sha,
                    "recorded_rolling_base_tree_sha": preparation.expected_base_tree_sha,
                }
            )
            guard.record_landed(
                admission=finalization.admission,
                entry=entry,
                observed_base_sha=result_sha,
                observed_base_tree_sha=preparation.expected_merge_tree_sha,
                base_contains_merge_commit=True,
                provider_effect_attempted=True,
                observed_at=utc_now().isoformat(),
            )
            checkpoint(entry)
            return entry
    except Exception as error:
        # A completed/unknown dispatch is durable before cleanup. Never rewrite
        # its consumed preparation because a later progress or cleanup step failed.
        try:
            current = store.read_ordinary_landing_preparation(
                preparation_id=preparation.preparation_id
            )
            if current.state in {"reserved", "observed"}:
                store.close_ordinary_landing_preparation(
                    preparation_id=current.preparation_id,
                    expected_revision=current.revision,
                    reason_code=_preparation_failure_reason(error),
                )
        except Exception as cleanup_error:
            # Cleanup failure is not a clean deferral. Retain both failures so
            # the worker can recover the reservation without claiming it closed.
            raise ExceptionGroup(
                "Landing attempt failed and preparation cleanup is unconfirmed",
                [error, cleanup_error],
            ) from None
        raise


def _preparation_failure_reason(
    error: Exception,
) -> Literal[
    "provider_wait", "provider_attempt_deadline", "evidence_denied", "process_interrupted"
]:
    if isinstance(error, OrdinaryAgentProviderDeferred):
        if error.reason_code == "provider_wait":
            return "provider_wait"
        if error.reason_code == "provider_attempt_deadline":
            return "provider_attempt_deadline"
    if isinstance(
        error,
        (
            OrdinaryAgentProviderEvidenceError,
            MergeAdmissionDeniedError,
            OrdinaryAgentSessionAdmissionDenied,
        ),
    ):
        return "evidence_denied"
    return "process_interrupted"
