from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from control_plane.change_impact_service import ChangeImpactRepositoryEvidenceProvider
from control_plane.contracts.change_impact import (
    ChangeImpactRepositoryEvidence,
    ChangeImpactTargetReference,
)
from control_plane.contracts.governance_projection import (
    GovernanceAdvisoryObservation,
    GovernanceLandingOutcomeFacet,
    GovernanceMergeAdmissionFacet,
    GovernanceMergeReadinessFacet,
    GovernanceOwnerHistoryEntry,
    GovernanceOwnerJudgmentFacet,
    GovernanceProjection,
)
from control_plane.contracts.merge_admission_record import (
    MergeAdmissionRecord,
)
from control_plane.contracts.merge_readiness import MergeReadinessResult
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceEventRecord,
    owner_acceptance_human_action_semantics,
)
from control_plane.merge_admission import (
    MergeAdmissionDeniedError,
    require_merge_admission_record_store,
)
from control_plane.merge_admission_live import LiveMergeAdmissionEvaluator
from control_plane.merge_train_batch_candidate import (
    require_merge_train_batch_candidate_record_store,
)
from control_plane.merge_train_batch_landing import (
    require_merge_train_batch_landing_plan_record_store,
)
from control_plane.merge_train_controller_run_once import (
    require_merge_train_controller_state_record_store,
)
from control_plane.merge_train_github import (
    MergeTrainGitHubError,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.owner_acceptance import (
    evaluate_owner_acceptance,
    require_owner_acceptance_event_store,
)
from control_plane.tenant_admission_controller import TenantAdmissionControllerGitHubClient
from control_plane.workflows.merge_train_controller import (
    latest_merge_train_batch_candidate_progress_record,
    latest_merge_train_batch_landing_progress_record,
)


class GovernanceCurrentReadinessProvider(Protocol):
    def __call__(
        self,
        *,
        store: object,
        repository_evidence: ChangeImpactRepositoryEvidence,
        base_branch: str,
        evaluated_at: str,
        github_token_env_var: str,
    ) -> GovernanceMergeReadinessFacet: ...


class GovernanceMergeTrainReadStore(Protocol):
    def list_merge_train_stack_collapse_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainStackCollapsePlanRecord, ...]: ...


@dataclass(frozen=True)
class LiveGovernanceCurrentReadinessProvider:
    github_token: Callable[[str], str]
    evaluator_factory: Callable[
        [object, ChangeImpactRepositoryEvidenceProvider, str],
        object,
    ] = lambda store, provider, token: LiveMergeAdmissionEvaluator(
        store=store,
        repository_evidence_provider=provider,
        technical_check_client=TenantAdmissionControllerGitHubClient(
            transport=UrllibMergeTrainGitHubTransport(token=token)
        ),
    )

    def __call__(
        self,
        *,
        store: object,
        repository_evidence: ChangeImpactRepositoryEvidence,
        base_branch: str,
        evaluated_at: str,
        github_token_env_var: str,
    ) -> GovernanceMergeReadinessFacet:
        repository = repository_evidence.target.repository
        pull_request_number = repository_evidence.target.pull_request_number
        try:
            landing_store = require_merge_train_batch_landing_plan_record_store(store)
            landing_records = landing_store.list_merge_train_batch_landing_plan_records(
                repository=repository,
                base_branch=base_branch,
                status="active",
                limit=100,
            )
            matching_landing_records = tuple(
                record
                for record in landing_records
                if any(
                    entry.pull_request_number == pull_request_number
                    for entry in record.landing_plan.entries
                )
            )
            landing_record = latest_merge_train_batch_landing_progress_record(
                matching_landing_records
            )
            if landing_record is None:
                return _inactive_readiness()
            entry = next(
                item
                for item in landing_record.landing_plan.entries
                if item.pull_request_number == pull_request_number
            )
            if entry.status in {"merged", "skipped", "stale", "blocked"}:
                return _inactive_readiness()
            candidate_store = require_merge_train_batch_candidate_record_store(store)
            candidate_matches = tuple(
                record
                for record in candidate_store.list_merge_train_batch_candidate_records(
                    repository=repository,
                    base_branch=base_branch,
                    status="active",
                    limit=100,
                )
                if record.candidate.batch_id == landing_record.landing_plan.batch_id
                and record.candidate.candidate_sha == landing_record.landing_plan.candidate_sha
                and record.candidate.candidate_sha256
                == landing_record.landing_plan.candidate_sha256
            )
            candidate_record = latest_merge_train_batch_candidate_progress_record(candidate_matches)
            if candidate_record is None:
                return _unavailable_readiness()
            controller_store = require_merge_train_controller_state_record_store(store)
            controller_records = controller_store.list_merge_train_controller_state_records(
                repository=repository,
                base_branch=base_branch,
                limit=1,
            )
            if not controller_records:
                return _unavailable_readiness()
            controller_state = controller_records[0]
            if controller_state.status != "running" or not controller_state.lease_owner:
                return _unavailable_readiness()
            token = self.github_token(github_token_env_var).strip()
            if not token:
                return _unavailable_readiness()
            stack_collapse_record = _stack_collapse_record(
                store=store,
                repository=repository,
                base_branch=base_branch,
                collapse_record_id=(
                    candidate_record.candidate.stack_collapse_root.collapse_record_id
                    if candidate_record.candidate.stack_collapse_root is not None
                    else ""
                ),
            )
            evaluator = cast(
                LiveMergeAdmissionEvaluator,
                self.evaluator_factory(
                    store,
                    _ResolvedRepositoryEvidenceProvider(repository_evidence),
                    token,
                ),
            )
            base = repository_evidence.base
            observed_base_sha = entry.recorded_rolling_base_sha or (base.base_sha if base else "")
            observed_base_tree_sha = (
                entry.recorded_rolling_base_tree_sha or entry.recorded_candidate_parent_tree_sha
            )
            if not observed_base_sha or not observed_base_tree_sha:
                return _unavailable_readiness()
            evaluation = evaluator.evaluate(
                candidate_record=candidate_record,
                landing_plan_record=landing_record,
                entry=entry,
                observed_base_sha=observed_base_sha,
                observed_base_tree_sha=observed_base_tree_sha,
                observed_head_sha=repository_evidence.target.head_sha,
                observed_head_tree_sha=repository_evidence.target.tree_sha,
                controller_state=controller_state,
                expected_lease_owner=controller_state.lease_owner,
                stack_collapse_record=stack_collapse_record,
                evaluated_at=evaluated_at,
            )
        except (
            LookupError,
            MergeAdmissionDeniedError,
            MergeTrainGitHubError,
            StopIteration,
            TypeError,
            ValueError,
        ):
            return _unavailable_readiness()
        return GovernanceMergeReadinessFacet(
            availability="available",
            reason_code="current_evaluation_available",
            result=evaluation.readiness,
        )


def build_governance_projection(
    *,
    store: object,
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider,
    current_readiness_provider: GovernanceCurrentReadinessProvider,
    target: ChangeImpactTargetReference,
    base_branch: str,
    generated_at: str,
    repository_evidence: ChangeImpactRepositoryEvidence | None = None,
    github_token_env_var: str = "",
) -> GovernanceProjection:
    resolved_repository_evidence = repository_evidence or repository_evidence_provider.resolve(
        target
    )
    snapshot_provider = _ResolvedRepositoryEvidenceProvider(resolved_repository_evidence)
    decision = evaluate_owner_acceptance(
        store=store,
        repository_evidence_provider=snapshot_provider,
        target=target,
        evaluated_at=generated_at,
    )
    history = require_owner_acceptance_event_store(store).list_owner_acceptance_event_records(
        repository_id=resolved_repository_evidence.target.repository_id,
        pull_request_number=resolved_repository_evidence.target.pull_request_number,
    )
    readiness = current_readiness_provider(
        store=store,
        repository_evidence=resolved_repository_evidence,
        base_branch=base_branch,
        evaluated_at=generated_at,
        github_token_env_var=github_token_env_var,
    )
    admission_store = require_merge_admission_record_store(store)
    admissions = admission_store.list_merge_admission_records(
        repository=resolved_repository_evidence.target.repository,
        base_branch=base_branch,
        pull_request_number=resolved_repository_evidence.target.pull_request_number,
        limit=100,
    )
    admission = admissions[0] if admissions else None
    admission_target_status = _admission_target_status(
        admission=admission,
        repository_evidence=resolved_repository_evidence,
    )
    outcomes = (
        admission_store.list_merge_landing_outcome_records(
            admission_id=admission.admission_id,
            limit=1,
        )
        if admission is not None
        else ()
    )
    outcome = outcomes[0] if outcomes else None
    return GovernanceProjection(
        target=resolved_repository_evidence.target,
        owner_judgment=GovernanceOwnerJudgmentFacet(
            current=decision,
            history=tuple(
                _owner_history_entry(
                    event,
                    repository_evidence=resolved_repository_evidence,
                    current_event_ids=_current_owner_event_ids(decision),
                )
                for event in history
            ),
        ),
        merge_readiness=readiness,
        merge_admission=GovernanceMergeAdmissionFacet(
            status=(
                "not_recorded"
                if admission is None
                else (
                    "admitted_current_target"
                    if admission_target_status == "current"
                    else "admitted_historical_target"
                )
            ),
            authorizes=("one_exact_merge_attempt",) if admission is not None else (),
            record=admission,
        ),
        landing_outcome=GovernanceLandingOutcomeFacet(
            target_status=admission_target_status if outcome is not None else "none",
            status=outcome.status if outcome is not None else "not_observed",
            landed=outcome is not None and outcome.status == "landed",
            record=outcome,
        ),
        advisory_observations=_advisory_observations(
            current_readiness=readiness.result,
            admission=admission,
        ),
        generated_at=generated_at,
    )


@dataclass(frozen=True, slots=True)
class _ResolvedRepositoryEvidenceProvider:
    evidence: ChangeImpactRepositoryEvidence

    def resolve(self, target: ChangeImpactTargetReference) -> ChangeImpactRepositoryEvidence:
        if (
            target.repository.lower() != self.evidence.target.repository.lower()
            or target.pull_request_number != self.evidence.target.pull_request_number
        ):
            raise LookupError("Resolved repository evidence does not match the requested target.")
        return self.evidence


def _owner_history_entry(
    event: OwnerAcceptanceEventRecord,
    *,
    repository_evidence: ChangeImpactRepositoryEvidence,
    current_event_ids: frozenset[str],
) -> GovernanceOwnerHistoryEntry:
    return GovernanceOwnerHistoryEntry(
        record=event,
        human_action_semantics=owner_acceptance_human_action_semantics(event.action),
        target_status=(
            "current"
            if event.binding.head_sha == repository_evidence.target.head_sha
            and event.binding.tree_sha == repository_evidence.target.tree_sha
            else "historical"
        ),
        decision_relationship=("current" if event.event_id in current_event_ids else "historical"),
    )


def _current_owner_event_ids(decision: object) -> frozenset[str]:
    event_ids: set[str] = set()
    current_event = getattr(decision, "current_event", None)
    if current_event is not None:
        event_ids.add(current_event.event_id)
    for product in getattr(decision, "products", ()):
        if product.current_event is not None:
            event_ids.add(product.current_event.event_id)
    return frozenset(event_ids)


def _admission_target_status(
    *,
    admission: MergeAdmissionRecord | None,
    repository_evidence: ChangeImpactRepositoryEvidence,
) -> Literal["current", "historical", "none"]:
    if admission is None:
        return "none"
    if (
        admission.pull_request_head_sha == repository_evidence.target.head_sha
        and admission.pull_request_head_tree_sha == repository_evidence.target.tree_sha
    ):
        return "current"
    return "historical"


def _inactive_readiness() -> GovernanceMergeReadinessFacet:
    return GovernanceMergeReadinessFacet(
        availability="not_active",
        reason_code="no_active_merge_lineage",
    )


def _unavailable_readiness() -> GovernanceMergeReadinessFacet:
    return GovernanceMergeReadinessFacet(
        availability="unavailable",
        reason_code="current_evidence_unavailable",
    )


def _stack_collapse_record(
    *,
    store: object,
    repository: str,
    base_branch: str,
    collapse_record_id: str,
) -> MergeTrainStackCollapsePlanRecord | None:
    if not collapse_record_id:
        return None
    method = getattr(store, "list_merge_train_stack_collapse_plan_records", None)
    if not callable(method):
        return None
    records = cast(
        GovernanceMergeTrainReadStore, store
    ).list_merge_train_stack_collapse_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=100,
    )
    return next(
        (record for record in records if getattr(record, "record_id", "") == collapse_record_id),
        None,
    )


def _advisory_observations(
    *,
    current_readiness: MergeReadinessResult | None,
    admission: MergeAdmissionRecord | None,
) -> tuple[GovernanceAdvisoryObservation, ...]:
    scope: Literal["current_readiness", "admission_readiness"] = "current_readiness"
    readiness = current_readiness
    if readiness is None and admission is not None:
        scope = "admission_readiness"
        readiness = admission.readiness
    if readiness is None:
        return ()
    return tuple(
        GovernanceAdvisoryObservation(
            observation_scope=scope,
            observation=observation,
        )
        for observation in readiness.technical_checks.advisory_observations
    )
