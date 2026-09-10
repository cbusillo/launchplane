"""Inactive ordinary-agent assembly for one bounded merge-train controller step."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import time
from typing import Protocol

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerLeaseLostError,
    MergeTrainControllerStateRecord,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_stack_collapse import MergeTrainStackCollapsePlanRecord
from control_plane.contracts.ordinary_agent_noop import OrdinaryAgentNoOpLandingStore
from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentCandidateCheckResult,
    OrdinaryAgentLandingEvidence,
    OrdinaryAgentMergeTrainSnapshotResult,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    is_guarded_ordinary_agent_finite_request,
)
from control_plane.github_app_identity import GitHubApiRequest
from control_plane.merge_admission import (
    GuardedMergeAdmission,
    MergeAdmissionDeniedError,
    MergeAdmissionEvaluation,
    MergeAdmissionRecordStore,
    MergeAdmissionReconciliationRequiredError,
)
from control_plane.merge_admission_live import LiveMergeAdmissionEvaluator
from control_plane.merge_train_controller_run_once import (
    DEFAULT_MERGE_TRAIN_CONTROLLER_LEASE_SECONDS,
    MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
    MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
    MergeTrainControllerRunOnceEnvelope,
    MergeTrainControllerRunOnceResult,
    execute_merge_train_controller_with_client,
)
from control_plane.merge_train_github import (
    MergeTrainGitHubError,
    MergeTrainGitHubStaleHeadError,
    MergeTrainGitHubTransport,
)
from control_plane.merge_train_policy_source import resolve_merge_train_policy_record
from control_plane.ordinary_agent_admission_store import OrdinaryAgentAdmissionAdapter
from control_plane.ordinary_agent_controller_store import (
    OrdinaryAgentControllerAdapter,
    OrdinaryAgentControllerReadStore,
    OrdinaryAgentProgressAdapter,
    OrdinaryAgentProgressReadStore,
)
from control_plane.ordinary_agent_custody import (
    OrdinaryAgentCustodyAttemptStore,
    OrdinaryAgentCustodyCleanupUnknown,
    OrdinaryAgentCustodySecretStore,
)
from control_plane.ordinary_agent_effect_reconciliation import reconcile_ordinary_effect_once
from control_plane.ordinary_agent_effect_recovery import recover_ordinary_effect
from control_plane.ordinary_agent_effect_router import (
    OrdinaryAgentEffectRouteDeferred,
    OrdinaryAgentSemanticEffectRouter,
)
from control_plane.ordinary_agent_github_transport import (
    OrdinaryAgentProviderDeferred,
    OrdinaryAgentProviderEvidenceError,
)
from control_plane.ordinary_agent_landing_evidence import (
    OrdinaryAgentLandingRepositoryEvidenceProvider,
    OrdinaryAgentLandingSnapshotReader,
    OrdinaryAgentLandingTechnicalCheckClient,
)
from control_plane.ordinary_agent_landing_execution import (
    OrdinaryLandingRecoveryRequired,
    execute_fresh_ordinary_landing,
)
from control_plane.ordinary_agent_landing_recovery import (
    OrdinaryLandingProgressReloadRequired,
    OrdinaryLandingRetryRequired,
    ordinary_landing_history_allows_retry,
    recover_ordinary_landing_entry,
)
from control_plane.ordinary_agent_merge_train_client import OrdinaryAgentMergeTrainClient
from control_plane.ordinary_agent_noop_route import (
    OrdinaryNoOpFinalizationUnavailable,
    OrdinaryNoOpLandingRoute,
)
from control_plane.ordinary_agent_merge_train_executor import (
    OrdinaryAgentEffectTerminal,
)
from control_plane.ordinary_agent_merge_train_snapshot import (
    OrdinaryAgentReadmissionRequired,
    acquire_ordinary_agent_candidate_check,
    acquire_ordinary_agent_merge_train_snapshot,
)
from control_plane.ordinary_agent_session_lifecycle import (
    OrdinaryAgentSessionAdmissionDenied,
)
from control_plane.ordinary_agent_snapshot_reader import (
    read_ordinary_candidate_check,
    read_ordinary_controller_snapshot,
)
from control_plane.repository_inventory import (
    RepositoryInventoryReadStore,
    get_repository_inventory_read_model,
)
from control_plane.workflows.launchplane import github_api_request


_PARK_SECONDS = effects.MIN_RECONCILIATION_BACKOFF_SECONDS
_CONTROLLER_BLOCK_REASONS = {
    "controller_authority_changed": "controller_busy",
    "controller_fence_rejected": "controller_busy",
    "landing_lineage_changed": "protection_changed",
    "landing_policy_not_admitted": "protection_changed",
    "merge_readiness_not_ready": "protection_changed",
    "structural_provenance_missing": "protection_changed",
    "structural_provenance_not_admitted": "protection_changed",
}
_AUTHORITY_REASONS = {
    "credential_expired",
    "credential_revoked",
    "custody_binding_conflict",
    "custody_profile_denied",
    "custody_unavailable",
    "lease_expired",
    "lease_revoked",
    "policy_unavailable",
    "principal_unavailable",
    "request_chain_mismatch",
    "session_expired",
    "session_revoked",
}
_EFFECT_BUDGET_REASONS = {
    "budget_exhausted",
    "custody_attempts_exhausted",
    "effect_attempts_exhausted",
    "reconciliation_exhausted",
}
_SNAPSHOT_EXHAUSTED_REASONS = {
    "read_attempts_exhausted",
    "read_recovery_evidence_unavailable",
    "source_check_observation_budget_exhausted",
}


class OrdinaryAgentMergeTrainJobStore(
    effects.OrdinaryAgentControllerStore,
    effects.OrdinaryAgentEffectStore,
    effects.OrdinaryAgentLandingStore,
    OrdinaryAgentNoOpLandingStore,
    effects.OrdinaryAgentSnapshotStore,
    effects.OrdinaryAgentReadmissionStore,
    OrdinaryAgentControllerReadStore,
    OrdinaryAgentProgressReadStore,
    OrdinaryAgentCustodyAttemptStore,
    OrdinaryAgentCustodySecretStore,
    MergeAdmissionRecordStore,
    RepositoryInventoryReadStore,
    Protocol,
):
    def list_merge_train_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[MergeTrainPolicyRecord, ...]: ...

    def read_ordinary_agent_job_recovery_snapshot(
        self, *, claim_fence: effects.OrdinaryAgentJobClaimFence
    ) -> effects.OrdinaryAgentJobRecoverySnapshot: ...


@dataclass
class _EvidenceBoundOrdinaryAdmissionEvaluator:
    store: object
    policy_record: MergeTrainPolicyRecord
    _evidence: OrdinaryAgentLandingEvidence | None = field(default=None, init=False)
    _delegate: LiveMergeAdmissionEvaluator | None = field(default=None, init=False)

    def bind(
        self,
        *,
        evidence: OrdinaryAgentLandingEvidence,
        candidate_record: MergeTrainBatchCandidateRecord,
        landing_plan_record: MergeTrainBatchLandingPlanRecord,
        entry: MergeTrainBatchLandingEntry,
    ) -> None:
        plan = landing_plan_record.landing_plan
        if (
            evidence.repository.lower() != plan.repository.lower()
            or evidence.base_ref != plan.base_branch
            or evidence.candidate_sha != plan.candidate_sha
            or evidence.repository_evidence.target.pull_request_number != entry.pull_request_number
            or candidate_record.candidate.candidate_sha != evidence.candidate_sha
        ):
            raise OrdinaryAgentSessionAdmissionDenied("landing_evidence_binding_conflict")
        if self._evidence is not None:
            if self._evidence != evidence:
                raise OrdinaryAgentSessionAdmissionDenied("landing_evidence_binding_conflict")
            return
        self._evidence = evidence
        self._delegate = LiveMergeAdmissionEvaluator(
            store=self.store,
            repository_evidence_provider=OrdinaryAgentLandingRepositoryEvidenceProvider(evidence),
            technical_check_client=OrdinaryAgentLandingTechnicalCheckClient(evidence),
            policy_record_provider=lambda: self.policy_record,
            snapshot_reader=OrdinaryAgentLandingSnapshotReader(
                repository=evidence.repository,
                base_branch=evidence.base_ref,
                snapshot=evidence.snapshot,
            ),
        )

    def evaluate(
        self,
        *,
        candidate_record: MergeTrainBatchCandidateRecord,
        landing_plan_record: MergeTrainBatchLandingPlanRecord,
        entry: MergeTrainBatchLandingEntry,
        observed_base_sha: str,
        observed_base_tree_sha: str,
        observed_head_sha: str,
        observed_head_tree_sha: str,
        controller_state: MergeTrainControllerStateRecord,
        expected_lease_owner: str,
        stack_collapse_record: MergeTrainStackCollapsePlanRecord | None,
        evaluated_at: str,
    ) -> MergeAdmissionEvaluation:
        if self._delegate is None:
            raise MergeAdmissionDeniedError(
                "Ordinary landing evidence has not been bound.",
                reason_code="landing_evidence_unavailable",
            )
        return self._delegate.evaluate(
            candidate_record=candidate_record,
            landing_plan_record=landing_plan_record,
            entry=entry,
            observed_base_sha=observed_base_sha,
            observed_base_tree_sha=observed_base_tree_sha,
            observed_head_sha=observed_head_sha,
            observed_head_tree_sha=observed_head_tree_sha,
            controller_state=controller_state,
            expected_lease_owner=expected_lease_owner,
            stack_collapse_record=stack_collapse_record,
            evaluated_at=evaluated_at,
        )


def advance_ordinary_agent_merge_train_job(
    *,
    claimed: effects.OrdinaryAgentClaimedJob,
    store: OrdinaryAgentMergeTrainJobStore,
    api_request: GitHubApiRequest = github_api_request,
    effect_transport_factory: Callable[[str], MergeTrainGitHubTransport] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> effects.OrdinaryAgentJobAttemptDisposition:
    """Advance one finite ordinary job step without registering a runtime worker."""
    request = claimed.request
    if not is_guarded_ordinary_agent_finite_request(request):
        raise OrdinaryAgentSessionAdmissionDenied("request_purpose_unsupported")
    snapshot = store.read_ordinary_agent_job_recovery_snapshot(claim_fence=claimed.claim_fence)
    _require_recovery_binding(claimed, snapshot)
    controller = OrdinaryAgentControllerAdapter(claimed=claimed, store=store, reader=store)

    try:
        routed = _route_existing_history(
            claimed=claimed,
            store=store,
            controller=controller,
            snapshot=snapshot,
            api_request=api_request,
            effect_transport_factory=effect_transport_factory,
            monotonic=monotonic,
            utc_now=utc_now,
        )
    except Exception as error:
        if controller.controller_acquired and not controller.yield_confirmed:
            raise
        try:
            refreshed = store.read_ordinary_agent_job_recovery_snapshot(
                claim_fence=claimed.claim_fence
            )
            _require_recovery_binding(claimed, refreshed)
        except Exception:
            raise error
        disposition = _expected_exception_disposition(error=error, snapshot=refreshed)
        if disposition is None:
            raise
        if not controller.controller_acquired and not refreshed.custody_uncertain:
            controller.release_terminal_history()
        return disposition
    if routed is not None:
        return routed

    terminal_at = request.continuation_expires_at or request.expires_at
    if request.cancellation_requested_at is not None or snapshot.observed_at >= terminal_at:
        store.retire_ordinary_agent_job_history(claim_fence=claimed.claim_fence)
        refreshed = store.read_ordinary_agent_job_recovery_snapshot(claim_fence=claimed.claim_fence)
        if refreshed.unresolved_effect is not None or refreshed.custody_uncertain:
            return _waiting(
                refreshed,
                reason_code="prior_effect_unresolved"
                if refreshed.unresolved_effect is not None
                else "custody_cleanup_required",
            )
        return _blocked(
            refreshed,
            reason_code="job_expired"
            if snapshot.observed_at >= terminal_at
            else "authority_unavailable",
        )

    if snapshot.custody_uncertain:
        return _waiting(snapshot, reason_code="custody_cleanup_required")
    preparation = snapshot.open_landing_preparation
    if preparation is not None and not (
        preparation.state == "terminal"
        and preparation.reason_code
        in {"provider_wait", "provider_attempt_deadline", "evidence_denied"}
        and preparation.attempt_ordinal < effects.MAX_ORDINARY_LANDING_ATTEMPTS_PER_ENTRY
        and preparation.effect_id is None
    ):
        # Crashed open attempts remain blocked. A gracefully closed attempt may
        # reach the retry reservation; that transaction rechecks custody and all
        # current authority before a token can be minted.
        controller.release_terminal_history()
        return _blocked(snapshot, reason_code="ordinary_readmission_required")
    if (
        snapshot.inspected_app_id is None
        or snapshot.inspected_app_id <= 0
        or snapshot.inspected_installation_id is None
        or snapshot.inspected_installation_id <= 0
    ):
        return _blocked(snapshot, reason_code="authority_unavailable")
    if (
        snapshot.provider_retry_not_before is not None
        and snapshot.provider_retry_not_before > snapshot.observed_at
    ):
        return _waiting(snapshot, reason_code="provider_wait")

    try:
        policy_record = resolve_merge_train_policy_record(store)
        repository_policy = policy_record.policy.find_repository_policy(
            repository=request.target.repository,
            base_branch=request.target.base_branch,
        )
        inventory = get_repository_inventory_read_model(
            repository_id=str(request.target.repository_id), store=store
        )
        current_inventory = inventory.current_record
        if (
            inventory.status != "available"
            or current_inventory is None
            or current_inventory.inventory_state != "tracked"
            or current_inventory.repository_id != str(request.target.repository_id)
            or current_inventory.repository.lower() != request.target.repository.lower()
        ):
            return _blocked(snapshot, reason_code="authority_unavailable")
        repository_owner_id = int(current_inventory.repository_owner_id)
        if repository_owner_id <= 0:
            return _blocked(snapshot, reason_code="authority_unavailable")
    except (LookupError, TypeError, ValueError):
        return _blocked(snapshot, reason_code="authority_unavailable")

    no_op_route = OrdinaryNoOpLandingRoute(store=store)
    progress = OrdinaryAgentProgressAdapter(
        controller=controller, reader=store, no_op_route=no_op_route
    )
    admissions = OrdinaryAgentAdmissionAdapter(
        controller=controller, progress=progress, reader=store
    )
    router = OrdinaryAgentSemanticEffectRouter(
        request=request,
        controller_fence=lambda: controller.acquired_fence,
        store=store,
        api_request=api_request,
        transport_factory=effect_transport_factory,
        monotonic=monotonic,
        utc_now=utc_now,
    )
    evaluator = _EvidenceBoundOrdinaryAdmissionEvaluator(
        store=store,
        policy_record=policy_record,
    )
    trace_id = "ordinary-job-" + canonical_json_sha256(claimed.claim_fence.model_dump(mode="json"))

    def read_snapshot() -> OrdinaryAgentMergeTrainSnapshotResult:
        return acquire_ordinary_agent_merge_train_snapshot(
            store=store,
            custody_store=store,
            secret_store=store,
            request_id=request.request_id,
            expected_binding_revision=request.binding_revision,
            controller_fence=controller.acquired_fence,
            reader=lambda transport: read_ordinary_controller_snapshot(
                transport=transport,
                request=request,
                repository_owner_id=repository_owner_id,
                repository_policy=repository_policy,
                utc_seconds=lambda: utc_now().timestamp(),
            ),
            api_request=api_request,
            monotonic=monotonic,
            utc_now=utc_now,
        )

    def read_candidate(candidate_sha: str) -> OrdinaryAgentCandidateCheckResult:
        return acquire_ordinary_agent_candidate_check(
            store=store,
            custody_store=store,
            secret_store=store,
            request_id=request.request_id,
            expected_binding_revision=request.binding_revision,
            controller_fence=controller.acquired_fence,
            candidate_sha=candidate_sha,
            reader=lambda transport: read_ordinary_candidate_check(
                transport=transport,
                request=request,
                repository_owner_id=repository_owner_id,
                candidate_sha=candidate_sha,
                utc_seconds=lambda: utc_now().timestamp(),
            ),
            api_request=api_request,
            monotonic=monotonic,
            utc_now=utc_now,
        )

    def current_controller() -> MergeTrainControllerStateRecord:
        records = controller.list_merge_train_controller_state_records(
            repository=request.target.repository,
            base_branch=request.target.base_branch,
            limit=2,
        )
        if len(records) != 1:
            raise MergeAdmissionDeniedError(
                "Ordinary controller state is unavailable.",
                reason_code="controller_fence_rejected",
            )
        return records[0]

    def advance_landing_entry(
        *,
        candidate_record: MergeTrainBatchCandidateRecord,
        landing_plan_record: MergeTrainBatchLandingPlanRecord,
        entry: MergeTrainBatchLandingEntry,
        semantic_ordinal: int,
        checkpoint: Callable[[MergeTrainBatchLandingEntry], None],
    ) -> MergeTrainBatchLandingEntry:
        def guard_factory(
            preparation: effects.OrdinaryAgentLandingPreparation,
            evidence: OrdinaryAgentLandingEvidence,
        ) -> GuardedMergeAdmission:
            if (
                preparation.request_id != request.request_id
                or preparation.binding_revision != request.binding_revision
                or preparation.scope_sha256 != request.scope_sha256
                or preparation.evidence != evidence
                or preparation.entry.pull_request_number != entry.pull_request_number
                or preparation.semantic_ordinal != semantic_ordinal
            ):
                raise OrdinaryAgentSessionAdmissionDenied("landing_evidence_binding_conflict")
            evaluator.bind(
                evidence=evidence,
                candidate_record=candidate_record,
                landing_plan_record=landing_plan_record,
                entry=entry,
            )
            controller_state = current_controller()
            return GuardedMergeAdmission(
                record_store=admissions,
                evaluator=evaluator,
                candidate_record=candidate_record,
                landing_plan_record=landing_plan_record,
                controller_state=controller_state,
                controller_state_provider=current_controller,
                admission_time_provider=lambda: utc_now().astimezone(timezone.utc).isoformat(),
                trace_id=trace_id,
            )

        def execute_attempt(
            predecessor_preparation_id: str | None = None,
        ) -> MergeTrainBatchLandingEntry:
            return execute_fresh_ordinary_landing(
                store=store,
                request_id=request.request_id,
                binding_revision=request.binding_revision,
                controller_fence=controller.acquired_fence,
                pull_request_number=entry.pull_request_number,
                semantic_ordinal=semantic_ordinal,
                candidate_record=candidate_record,
                landing_plan_record=landing_plan_record,
                repository_owner_id=repository_owner_id,
                repository_policy=repository_policy,
                guard_factory=guard_factory,
                checkpoint=checkpoint,
                no_op_route=no_op_route,
                predecessor_preparation_id=predecessor_preparation_id,
                api_request=api_request,
                transport_factory=effect_transport_factory,
                monotonic=monotonic,
                utc_now=utc_now,
            )

        try:
            return execute_attempt()
        except OrdinaryLandingRecoveryRequired as recovery:
            try:
                return recover_ordinary_landing_entry(
                    store=store,
                    request=request,
                    preparation_id=recovery.preparation_id,
                    candidate_record=candidate_record,
                    landing_plan_record=landing_plan_record,
                    guard_factory=guard_factory,
                    checkpoint=checkpoint,
                )
            except OrdinaryLandingRetryRequired as retry:
                # At most one fresh successor executes in this poll. A replay or
                # another pre-send deferral leaves recovery to the next claim.
                return execute_attempt(retry.predecessor_preparation_id)

    client = OrdinaryAgentMergeTrainClient(
        request=request,
        effect_executor=router,
        snapshot=read_snapshot,
        candidate_check=read_candidate,
        advance_landing_entry=advance_landing_entry,
        advance_no_op_landing_entry=advance_landing_entry,
    )
    try:
        result = execute_merge_train_controller_with_client(
            request=MergeTrainControllerRunOnceEnvelope(
                repository=request.target.repository,
                base_branch=request.target.base_branch,
                mutate=True,
            ),
            policy=policy_record.policy,
            policy_sha256=policy_record.policy_sha256,
            repository_policy=repository_policy,
            github_client=client,
            trace_id=trace_id,
            recorded_at=utc_now().astimezone(timezone.utc).isoformat(),
            candidate_store=progress,
            landing_store=progress,
            stack_collapse_store=progress,
            controller_state_store=controller,
            admission_store=admissions,
            admission_evaluator=evaluator,
        )
    except OrdinaryAgentReadmissionRequired as readmission:
        if not controller.controller_acquired or not controller.yield_confirmed:
            raise
        try:
            store.finalize_ordinary_agent_readmission(
                claim_fence=claimed.claim_fence,
                expected_binding_revision=claimed.request.binding_revision,
                read_attempt_id=readmission.attempt_id,
                expected_observation_sha256=readmission.observation_sha256,
            )
        except OrdinaryAgentSessionAdmissionDenied as error:
            if error.reason_code in {
                "refresh_allowance_exhausted",
                "refresh_progress_must_be_retired",
            }:
                return _blocked(snapshot, reason_code="ordinary_readmission_required")
            raise
        # Source evidence and the yielded empty controller were consumed together.
        # The next claim must reload the new request binding.
        return _waiting(snapshot)
    except Exception as error:
        if isinstance(error, MergeTrainControllerLeaseLostError):
            raise
        if controller.controller_acquired and not controller.yield_confirmed:
            raise
        try:
            refreshed = store.read_ordinary_agent_job_recovery_snapshot(
                claim_fence=claimed.claim_fence
            )
            _require_recovery_binding(claimed, refreshed)
        except Exception:
            raise error
        disposition = _expected_exception_disposition(
            error=error,
            snapshot=refreshed,
        )
        if disposition is None:
            raise
        return disposition

    if not controller.yield_confirmed:
        raise RuntimeError("ordinary controller returned without a confirmed joined yield")
    return _result_disposition(
        result=result,
        claimed=claimed,
        store=store,
    )


def _route_existing_history(
    *,
    claimed: effects.OrdinaryAgentClaimedJob,
    store: OrdinaryAgentMergeTrainJobStore,
    controller: OrdinaryAgentControllerAdapter,
    snapshot: effects.OrdinaryAgentJobRecoverySnapshot,
    api_request: GitHubApiRequest,
    effect_transport_factory: Callable[[str], MergeTrainGitHubTransport] | None,
    monotonic: Callable[[], float],
    utc_now: Callable[[], datetime],
) -> effects.OrdinaryAgentJobAttemptDisposition | None:
    request = claimed.request
    if not is_guarded_ordinary_agent_finite_request(request):
        raise OrdinaryAgentSessionAdmissionDenied("request_purpose_unsupported")
    history = snapshot.unresolved_effect or snapshot.latest_unadvanced_effect
    if history is None:
        return None
    recovery = recover_ordinary_effect(history)
    if recovery.disposition == "observe":
        due = history.effect.next_observation_at
        if due is not None and due > snapshot.observed_at:
            controller.release_terminal_history()
            return _waiting(snapshot, reason_code="prior_effect_unresolved")
        reconcile_ordinary_effect_once(
            store=store,
            request=request,
            effect_id=history.effect.effect_id,
            api_request=api_request,
            transport_factory=effect_transport_factory,
            monotonic=monotonic,
            utc_now=utc_now,
        )
        controller.release_terminal_history()
        refreshed = store.read_ordinary_agent_job_recovery_snapshot(claim_fence=claimed.claim_fence)
        _require_recovery_binding(claimed, refreshed)
        if refreshed.unresolved_effect is not None:
            next_recovery = recover_ordinary_effect(refreshed.unresolved_effect)
            if next_recovery.disposition == "terminal":
                return _blocked(
                    refreshed,
                    reason_code=_terminal_reason(next_recovery.reason_code),
                )
        return _waiting(refreshed, reason_code="prior_effect_unresolved")
    if recovery.disposition == "rebind":
        if (
            history.effect.command.kind != "pull_request_head_refresh"
            or history.effect.state != "rebind_pending"
        ):
            controller.release_terminal_history()
            return _blocked(snapshot, reason_code="ordinary_readmission_required")
        return _rebind_completed_head_refresh(
            claimed=claimed, store=store, controller=controller, snapshot=snapshot, history=history
        )
    if recovery.disposition == "retry" and history.effect.command.kind == "pull_request_landing":
        if ordinary_landing_history_allows_retry(history):
            return None
        controller.release_terminal_history()
        return _blocked(snapshot, reason_code="ordinary_readmission_required")
    if recovery.disposition == "terminal":
        controller.release_terminal_history()
        return _blocked(snapshot, reason_code=_terminal_reason(recovery.reason_code))
    return None


def _rebind_completed_head_refresh(
    *,
    claimed: effects.OrdinaryAgentClaimedJob,
    store: OrdinaryAgentMergeTrainJobStore,
    controller: OrdinaryAgentControllerAdapter,
    snapshot: effects.OrdinaryAgentJobRecoverySnapshot,
    history: effects.OrdinaryAgentEffectHistory,
) -> effects.OrdinaryAgentJobAttemptDisposition:
    target = claimed.request.target
    try:
        policy_record = resolve_merge_train_policy_record(store)
        repository_policy = policy_record.policy.find_repository_policy(
            repository=target.repository, base_branch=target.base_branch
        )
    except (LookupError, ValueError):
        controller.release_terminal_history()
        return _blocked(snapshot, reason_code="authority_unavailable")
    try:
        controller.acquire_merge_train_controller_state_record(
            repository=target.repository,
            base_branch=target.base_branch,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_record.policy_sha256,
            lease_owner="",
            lease_seconds=DEFAULT_MERGE_TRAIN_CONTROLLER_LEASE_SECONDS,
            initial_active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
            initial_active_phase="select_next_action",
            adoptable_active_actions=MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
        )
        store.rebind_ordinary_agent_after_head_refresh(
            effect_id=history.effect.effect_id,
            expected_effect_revision=history.effect.revision,
            controller_fence=controller.acquired_fence,
        )
    except Exception as error:
        if controller.controller_acquired:
            try:
                controller.yield_acquired()
            except Exception as yield_error:
                raise ExceptionGroup(
                    "head-refresh recovery and controller yield failed", [error, yield_error]
                ) from None
        if isinstance(error, OrdinaryAgentSessionAdmissionDenied):
            disposition = None
            if error.reason_code in {
                "controller_busy",
                "target_busy",
                "controller_action_conflict",
            }:
                disposition = _waiting(snapshot, reason_code="controller_busy")
            elif error.reason_code in {"record_predecessor_conflict", "prior_effect_unresolved"}:
                disposition = _waiting(snapshot, reason_code="prior_effect_unresolved")
            elif error.reason_code in {
                "controller_policy_changed",
                "refresh_allowance_exhausted",
                "refresh_progress_must_be_retired",
            }:
                disposition = _blocked(snapshot, reason_code="ordinary_readmission_required")
            if disposition is not None:
                if not controller.controller_acquired:
                    controller.release_terminal_history()
                return disposition
        raise
    # The joined store already committed N+1 and an empty controller yield.
    # Finish this old-binding attempt without rereading through N's adapters.
    return _waiting(snapshot)


def _result_disposition(
    *,
    result: MergeTrainControllerRunOnceResult,
    claimed: effects.OrdinaryAgentClaimedJob,
    store: OrdinaryAgentMergeTrainJobStore,
) -> effects.OrdinaryAgentJobAttemptDisposition:
    accepted = result.accepted_result
    refreshed = store.read_ordinary_agent_job_recovery_snapshot(claim_fence=claimed.claim_fence)
    _require_recovery_binding(claimed, refreshed)
    landing_progress = accepted.get("landing_progress")
    if landing_progress == "complete":
        if refreshed.unresolved_effect is not None or refreshed.custody_uncertain:
            return _waiting(refreshed, reason_code="prior_effect_unresolved")
        if refreshed.open_landing_preparation is not None:
            return _blocked(refreshed, reason_code="ordinary_readmission_required")
        if refreshed.latest_unadvanced_effect is not None:
            recovery = recover_ordinary_effect(refreshed.latest_unadvanced_effect)
            if recovery.disposition == "retry":
                return _waiting(refreshed, reason_code="prior_effect_unresolved")
            if recovery.disposition == "rebind":
                return _blocked(refreshed, reason_code="ordinary_readmission_required")
            if recovery.disposition == "terminal":
                return _blocked(refreshed, reason_code=_terminal_reason(recovery.reason_code))
            raise RuntimeError("ordinary completion has unsupported unadvanced effect history")
        return effects.OrdinaryAgentJobAttemptDisposition(status="completed")
    if landing_progress in {"partial", "cleanup_pending"}:
        return _waiting(refreshed)
    if accepted.get("candidate_ref_cleanup_status") == "failed":
        reason = (
            "custody_cleanup_required" if refreshed.custody_uncertain else "prior_effect_unresolved"
        )
        return _waiting(refreshed, reason_code=reason)
    if (
        accepted.get("mode") == "stale_landing"
        or accepted.get("controller_action") == "candidate_failed"
    ):
        return _blocked(refreshed, reason_code="ordinary_readmission_required")
    action = accepted.get("controller_action")
    if action in {
        "plan_stack_collapse",
        "execute_stack_collapse",
        "stack_unsupported",
    } or isinstance(accepted.get("stack_collapse_plan"), dict):
        return _blocked(refreshed, reason_code="ordinary_stack_unsupported")
    if action == "idle":
        # The finite request's captured PR set produced no currently eligible
        # queue entry. Only a fresh admission may interpret that lifecycle drift;
        # candidate no-op finalization is handled inside the landing callback.
        return _blocked(refreshed, reason_code="ordinary_readmission_required")
    if accepted.get("mode") == "blocked":
        blocking = accepted.get("blocking_reason")
        code = blocking.get("code") if isinstance(blocking, dict) else None
        mapped = _CONTROLLER_BLOCK_REASONS.get(code) if isinstance(code, str) else None
        if mapped is None:
            raise RuntimeError("ordinary controller returned an unsupported blocking reason")
        return _blocked(refreshed, reason_code=mapped)
    return _waiting(refreshed)


def _expected_exception_disposition(
    *,
    error: Exception,
    snapshot: effects.OrdinaryAgentJobRecoverySnapshot,
) -> effects.OrdinaryAgentJobAttemptDisposition | None:
    mapped: effects.OrdinaryAgentJobAttemptDisposition | None
    if isinstance(error, OrdinaryAgentEffectRouteDeferred):
        mapped = (
            _waiting(snapshot, reason_code="prior_effect_unresolved")
            if error.disposition == "observe"
            else _blocked(snapshot, reason_code="ordinary_readmission_required")
        )
    elif isinstance(error, MergeTrainGitHubStaleHeadError):
        mapped = _blocked(snapshot, reason_code="ordinary_readmission_required")
    elif isinstance(error, OrdinaryAgentEffectTerminal):
        mapped = _blocked(
            snapshot,
            reason_code=(
                "ordinary_stack_unsupported"
                if error.reason_code == "ordinary_effect_method_unsupported"
                else _terminal_reason(error.reason_code)
            ),
        )
    elif isinstance(error, OrdinaryLandingProgressReloadRequired):
        mapped = _waiting(snapshot)
    elif isinstance(error, OrdinaryLandingRecoveryRequired):
        mapped = _blocked(snapshot, reason_code="prior_effect_unresolved")
    elif isinstance(error, MergeAdmissionReconciliationRequiredError):
        mapped = _waiting(snapshot, reason_code="prior_effect_unresolved")
    elif isinstance(error, OrdinaryAgentProviderDeferred):
        mapped = _waiting(
            snapshot,
            reason_code="provider_wait"
            if error.reason_code == "provider_wait"
            else "snapshot_unavailable",
            extra_deadline=error.retry_not_before,
        )
    elif isinstance(error, OrdinaryAgentCustodyCleanupUnknown):
        mapped = _waiting(snapshot, reason_code="custody_cleanup_required")
    elif isinstance(error, OrdinaryAgentProviderEvidenceError):
        mapped = _waiting(snapshot, reason_code="snapshot_unavailable")
    elif isinstance(error, OrdinaryAgentSessionAdmissionDenied):
        if error.reason_code in {
            "provider_wait",
            "provider_attempt_deadline",
            "source_check_wait",
            "candidate_check_wait",
        }:
            mapped = _waiting(
                snapshot,
                reason_code="provider_wait"
                if error.reason_code == "provider_wait"
                else "snapshot_unavailable",
                extra_deadline=error.retry_not_before,
            )
        elif error.reason_code in _SNAPSHOT_EXHAUSTED_REASONS:
            mapped = _blocked(snapshot, reason_code="snapshot_unavailable")
        elif error.reason_code == "candidate_check_observation_budget_exhausted":
            mapped = _blocked(snapshot, reason_code="candidate_check_observation_budget_exhausted")
        elif error.reason_code in {"cleanup_unknown", "read_custody_fenced"}:
            mapped = _waiting(snapshot, reason_code="custody_cleanup_required")
        elif error.reason_code in _EFFECT_BUDGET_REASONS:
            mapped = _blocked(snapshot, reason_code="effect_budget_exhausted")
        elif error.reason_code in _AUTHORITY_REASONS:
            mapped = _blocked(snapshot, reason_code="authority_unavailable")
        elif error.reason_code == "prior_effect_unresolved":
            mapped = _waiting(snapshot, reason_code="prior_effect_unresolved")
        else:
            return None
    elif isinstance(error, OrdinaryNoOpFinalizationUnavailable):
        mapped = _blocked(snapshot, reason_code="ordinary_noop_finalization_required")
    elif isinstance(error, MergeTrainGitHubError):
        mapped = _waiting(snapshot, reason_code="snapshot_unavailable")
    else:
        return None

    if snapshot.unresolved_effect is not None:
        recovery = recover_ordinary_effect(snapshot.unresolved_effect)
        if recovery.disposition == "observe":
            return _waiting(snapshot, reason_code="prior_effect_unresolved")
        if recovery.disposition == "rebind":
            return _blocked(snapshot, reason_code="ordinary_readmission_required")
        if recovery.disposition == "terminal":
            return _blocked(snapshot, reason_code=_terminal_reason(recovery.reason_code))
    if snapshot.custody_uncertain:
        return _waiting(snapshot, reason_code="custody_cleanup_required")
    return mapped


def _require_recovery_binding(
    claimed: effects.OrdinaryAgentClaimedJob,
    snapshot: effects.OrdinaryAgentJobRecoverySnapshot,
) -> None:
    request = claimed.request
    if (
        snapshot.request_id != request.request_id
        or snapshot.scope_sha256 != request.scope_sha256
        or snapshot.binding_revision != request.binding_revision
    ):
        raise OrdinaryAgentSessionAdmissionDenied("job_recovery_binding_conflict")


def _next_due(
    snapshot: effects.OrdinaryAgentJobRecoverySnapshot,
    extra_deadline: int | None = None,
) -> int:
    values = [
        snapshot.observed_at + 1,
        snapshot.pending_read_retry_not_before or 0,
        snapshot.provider_retry_not_before or 0,
        extra_deadline or 0,
    ]
    if snapshot.unresolved_effect is not None:
        values.append(snapshot.unresolved_effect.effect.next_observation_at or 0)
    return max(values)


def _waiting(
    snapshot: effects.OrdinaryAgentJobRecoverySnapshot,
    *,
    reason_code: str | None = None,
    extra_deadline: int | None = None,
) -> effects.OrdinaryAgentJobAttemptDisposition:
    return effects.OrdinaryAgentJobAttemptDisposition(
        status="waiting",
        next_due_at=_next_due(snapshot, extra_deadline),
        reason_code=reason_code,
    )


def _blocked(
    snapshot: effects.OrdinaryAgentJobRecoverySnapshot,
    *,
    reason_code: str,
) -> effects.OrdinaryAgentJobAttemptDisposition:
    return effects.OrdinaryAgentJobAttemptDisposition(
        status="blocked",
        next_due_at=_next_due(snapshot, snapshot.observed_at + _PARK_SECONDS),
        reason_code=reason_code,
    )


def _terminal_reason(reason_code: str | None) -> str:
    return (
        "effect_budget_exhausted"
        if reason_code in _EFFECT_BUDGET_REASONS
        or reason_code in {"effect_history_evidence_unavailable", "reconciliation_exhausted"}
        else "prior_effect_unresolved"
    )
