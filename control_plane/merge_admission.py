from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from control_plane.contracts.merge_admission_record import (
    MergeAdmissionFenceRejectedError,
    MergeAdmissionRecord,
    MergeLandingOutcomeReason,
    MergeLandingOutcomeRecord,
    build_merge_effect_attempt_id,
)
from control_plane.contracts.merge_readiness import MergeReadinessResult
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainStructuralCandidateResult,
)


MERGE_ADMISSION_ALGORITHM_VERSION = "merge-admission-v1"


def _utc_now_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


class MergeAdmissionDeniedError(ValueError):
    """Raised when fresh L2 or structural evidence refuses an effect attempt."""


class MergeAdmissionReconciliationRequiredError(RuntimeError):
    """Raised when an earlier attempt remains effect-ambiguous."""


class MergeAdmissionRecordStore(Protocol):
    def create_merge_admission_record_if_absent(
        self, record: MergeAdmissionRecord
    ) -> tuple[MergeAdmissionRecord, bool]: ...

    def create_guarded_merge_admission_record_if_absent(
        self,
        record: MergeAdmissionRecord,
        *,
        admitted_at: str,
    ) -> tuple[MergeAdmissionRecord, bool]: ...

    def read_merge_admission_record(self, admission_id: str) -> MergeAdmissionRecord: ...

    def list_merge_admission_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pull_request_number: int | None = None,
        landing_plan_record_id: str = "",
        landing_plan_id: str = "",
        attempt_id: str = "",
        limit: int | None = None,
    ) -> tuple[MergeAdmissionRecord, ...]: ...

    def create_merge_landing_outcome_record_if_absent(
        self, record: MergeLandingOutcomeRecord
    ) -> tuple[MergeLandingOutcomeRecord, bool]: ...

    def list_merge_landing_outcome_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pull_request_number: int | None = None,
        admission_id: str = "",
        status: str = "",
        observation_sequence: int | None = None,
        limit: int | None = None,
    ) -> tuple[MergeLandingOutcomeRecord, ...]: ...


def require_merge_admission_record_store(record_store: object) -> MergeAdmissionRecordStore:
    required_methods = (
        "create_merge_admission_record_if_absent",
        "create_guarded_merge_admission_record_if_absent",
        "read_merge_admission_record",
        "list_merge_admission_records",
        "create_merge_landing_outcome_record_if_absent",
        "list_merge_landing_outcome_records",
    )
    if all(callable(getattr(record_store, method_name, None)) for method_name in required_methods):
        return cast(MergeAdmissionRecordStore, record_store)
    raise TypeError("record store does not support guarded merge admission records")


@dataclass(frozen=True)
class MergeAdmissionEvaluation:
    readiness: MergeReadinessResult
    structural_result: MergeTrainStructuralCandidateResult


class MergeAdmissionEvaluator(Protocol):
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
        stack_collapse_record: MergeTrainStackCollapsePlanRecord | None,
        evaluated_at: str,
    ) -> MergeAdmissionEvaluation: ...


@dataclass
class GuardedMergeAdmission:
    record_store: MergeAdmissionRecordStore
    evaluator: MergeAdmissionEvaluator
    candidate_record: MergeTrainBatchCandidateRecord
    landing_plan_record: MergeTrainBatchLandingPlanRecord
    controller_state: MergeTrainControllerStateRecord
    trace_id: str
    admission_algorithm_version: str = MERGE_ADMISSION_ALGORITHM_VERSION
    controller_state_provider: Callable[[], MergeTrainControllerStateRecord] | None = None
    admission_time_provider: Callable[[], str] = _utc_now_timestamp
    stack_collapse_record: MergeTrainStackCollapsePlanRecord | None = None

    def update_landing_plan_record(
        self, landing_plan_record: MergeTrainBatchLandingPlanRecord
    ) -> None:
        self.landing_plan_record = landing_plan_record

    def update_landing_plan(self, landing_plan: MergeTrainBatchLandingPlan) -> None:
        self.landing_plan_record = self.landing_plan_record.model_copy(
            update={"landing_plan": landing_plan}
        )

    def admit(
        self,
        *,
        entry: MergeTrainBatchLandingEntry,
        observed_base_sha: str,
        observed_base_tree_sha: str,
        observed_head_sha: str,
        observed_head_tree_sha: str,
    ) -> MergeAdmissionRecord:
        if self.controller_state_provider is not None:
            self.controller_state = self.controller_state_provider()
        evaluation_started_at = self.admission_time_provider()
        existing = self.record_store.list_merge_admission_records(
            repository=self.landing_plan_record.landing_plan.repository,
            base_branch=self.landing_plan_record.landing_plan.base_branch,
            pull_request_number=entry.pull_request_number,
            landing_plan_id=self.landing_plan_record.landing_plan.plan_id,
        )
        if existing:
            latest_outcomes = self.record_store.list_merge_landing_outcome_records(
                admission_id=existing[0].admission_id,
                limit=1,
            )
            if not latest_outcomes or latest_outcomes[0].status == "reconcile_required":
                raise MergeAdmissionReconciliationRequiredError(
                    "A prior merge admission remains effect-ambiguous and must be reconciled."
                )
        attempt_sequence = max((record.attempt_sequence for record in existing), default=0) + 1
        evaluation = self.evaluator.evaluate(
            candidate_record=self.candidate_record,
            landing_plan_record=self.landing_plan_record,
            entry=entry,
            observed_base_sha=observed_base_sha,
            observed_base_tree_sha=observed_base_tree_sha,
            observed_head_sha=observed_head_sha,
            observed_head_tree_sha=observed_head_tree_sha,
            controller_state=self.controller_state,
            stack_collapse_record=self.stack_collapse_record,
            evaluated_at=evaluation_started_at,
        )
        if evaluation.readiness.state != "ready":
            raise MergeAdmissionDeniedError(
                "Fresh merge readiness evidence did not admit the provider effect."
            )
        if evaluation.structural_result.status not in {"exact", "recorded_rolling"}:
            raise MergeAdmissionDeniedError(
                "Fresh structural provenance did not admit the provider effect."
            )
        admitted_at = self.admission_time_provider()
        if self.controller_state_provider is not None:
            self.controller_state = self.controller_state_provider()
        landing_plan = self.landing_plan_record.landing_plan
        candidate = self.candidate_record.candidate
        provenance = candidate.structural_provenance
        if provenance is None:
            raise MergeAdmissionDeniedError("Merge admission requires structural provenance.")
        attempt_id = build_merge_effect_attempt_id(
            controller_key=self.controller_state.controller_key,
            lease_owner=self.controller_state.lease_owner,
            lease_acquired_at=self.controller_state.lease_acquired_at,
            landing_plan_id=landing_plan.plan_id,
            pull_request_number=entry.pull_request_number,
            queue_position=entry.position,
            attempt_sequence=attempt_sequence,
            expected_effect_sha=landing_plan.candidate_sha,
        )
        try:
            admission = MergeAdmissionRecord(
                attempt_id=attempt_id,
                attempt_sequence=attempt_sequence,
                source=f"service:merge-admission:{self.trace_id}",
                repository=landing_plan.repository,
                base_branch=landing_plan.base_branch,
                pull_request_number=entry.pull_request_number,
                queue_position=entry.position,
                batch_id=landing_plan.batch_id,
                candidate_record_id=self.candidate_record.record_id,
                landing_plan_record_id=self.landing_plan_record.record_id,
                landing_plan_id=landing_plan.plan_id,
                merge_method=entry.merge_method,
                effective_base_sha=observed_base_sha,
                effective_base_tree_sha=observed_base_tree_sha,
                pull_request_head_sha=observed_head_sha,
                pull_request_head_tree_sha=observed_head_tree_sha,
                candidate_sha=landing_plan.candidate_sha,
                candidate_tree_sha=landing_plan.candidate_tree_sha,
                expected_effect_sha=landing_plan.candidate_sha,
                candidate_sha256=landing_plan.candidate_sha256,
                structural_provenance_sha256=landing_plan.structural_provenance_sha256,
                landing_plan_sha256=landing_plan.landing_plan_sha256,
                readiness=evaluation.readiness,
                structural_result=evaluation.structural_result,
                admission_algorithm_version=self.admission_algorithm_version,
                controller_key=self.controller_state.controller_key,
                lease_owner=self.controller_state.lease_owner,
                lease_acquired_at=self.controller_state.lease_acquired_at,
                lease_expires_at=self.controller_state.lease_expires_at,
                created_at=admitted_at,
            )
        except ValueError as error:
            raise MergeAdmissionDeniedError(
                "Persisted controller authority changed after live merge evaluation."
            ) from error
        try:
            stored, created = self.record_store.create_guarded_merge_admission_record_if_absent(
                admission,
                admitted_at=admitted_at,
            )
        except MergeAdmissionFenceRejectedError as error:
            raise MergeAdmissionDeniedError(str(error)) from error
        if not created:
            raise MergeAdmissionReconciliationRequiredError(
                "Existing merge admission cannot be reused for another provider effect."
            )
        return stored

    def record_landed(
        self,
        *,
        admission: MergeAdmissionRecord,
        entry: MergeTrainBatchLandingEntry,
        observed_base_sha: str,
        observed_base_tree_sha: str,
        base_contains_merge_commit: bool,
        provider_effect_attempted: bool,
        observed_at: str,
    ) -> MergeLandingOutcomeRecord:
        outcome = self._outcome(
            admission=admission,
            status="landed",
            reason="provider_and_git_confirmed",
            provider_effect_attempted=provider_effect_attempted,
            observed_pull_request_head_sha=entry.landed_head_sha,
            observed_pull_request_head_tree_sha=entry.landed_head_tree_sha,
            observed_base_sha=observed_base_sha,
            observed_base_tree_sha=observed_base_tree_sha,
            merge_commit_sha=entry.merge_commit_sha,
            merge_commit_tree_sha=entry.merge_commit_tree_sha,
            base_contains_merge_commit=base_contains_merge_commit,
            exact_landing_confirmed=base_contains_merge_commit,
            observed_at=observed_at,
        )
        return self.record_store.create_merge_landing_outcome_record_if_absent(outcome)[0]

    def reconcile_existing_landed(
        self,
        *,
        entry: MergeTrainBatchLandingEntry,
        observed_base_sha: str,
        observed_base_tree_sha: str,
        provider_effect_attempted: bool,
        observed_at: str,
    ) -> MergeLandingOutcomeRecord:
        admissions = self.record_store.list_merge_admission_records(
            repository=self.landing_plan_record.landing_plan.repository,
            base_branch=self.landing_plan_record.landing_plan.base_branch,
            pull_request_number=entry.pull_request_number,
            landing_plan_id=self.landing_plan_record.landing_plan.plan_id,
        )
        if not admissions:
            raise MergeAdmissionReconciliationRequiredError(
                "Observed landing has no preceding immutable merge admission."
            )
        admission = admissions[0]
        outcomes = self.record_store.list_merge_landing_outcome_records(
            admission_id=admission.admission_id,
            limit=1,
        )
        if outcomes and outcomes[0].status == "landed":
            return outcomes[0]
        if outcomes and outcomes[0].status == "rejected":
            raise MergeAdmissionReconciliationRequiredError(
                "Provider landing evidence contradicts a conclusive rejection outcome."
            )
        return self.record_landed(
            admission=admission,
            entry=entry,
            observed_base_sha=observed_base_sha,
            observed_base_tree_sha=observed_base_tree_sha,
            base_contains_merge_commit=True,
            provider_effect_attempted=provider_effect_attempted,
            observed_at=observed_at,
        )

    def reconcile_existing_no_effect(
        self,
        *,
        entry: MergeTrainBatchLandingEntry,
        observed_base_sha: str,
        observed_base_tree_sha: str,
        observed_head_sha: str,
        observed_head_tree_sha: str,
        observed_pull_request_state: str,
        observed_at: str,
    ) -> MergeLandingOutcomeRecord | None:
        admissions = self.record_store.list_merge_admission_records(
            repository=self.landing_plan_record.landing_plan.repository,
            base_branch=self.landing_plan_record.landing_plan.base_branch,
            pull_request_number=entry.pull_request_number,
            landing_plan_id=self.landing_plan_record.landing_plan.plan_id,
        )
        if not admissions:
            return None
        admission = admissions[0]
        outcomes = self.record_store.list_merge_landing_outcome_records(
            admission_id=admission.admission_id,
            limit=1,
        )
        if outcomes and outcomes[0].status == "rejected":
            return outcomes[0]
        if outcomes and outcomes[0].status == "landed":
            raise MergeAdmissionReconciliationRequiredError(
                "Open pull request evidence contradicts a landed outcome."
            )
        if not outcomes:
            interrupted = self._outcome(
                admission=admission,
                status="reconcile_required",
                reason="process_interrupted",
                provider_effect_attempted=True,
                provider_message=(
                    "Admission existed without a terminal provider observation; exact provider "
                    "and Git evidence was re-read before no-effect reconciliation."
                ),
                observed_pull_request_state=observed_pull_request_state,
                observed_pull_request_head_sha=observed_head_sha,
                observed_pull_request_head_tree_sha=observed_head_tree_sha,
                observed_base_sha=observed_base_sha,
                observed_base_tree_sha=observed_base_tree_sha,
                observed_at=observed_at,
            )
            self.record_store.create_merge_landing_outcome_record_if_absent(interrupted)
        outcome = self._outcome(
            admission=admission,
            status="rejected",
            reason="reconciliation_confirmed_no_effect",
            provider_effect_attempted=True,
            provider_message=(
                "Fresh provider observation confirmed the exact pull request remains open "
                "and the expected base has not advanced."
            ),
            observed_pull_request_state=observed_pull_request_state,
            observed_pull_request_head_sha=observed_head_sha,
            observed_pull_request_head_tree_sha=observed_head_tree_sha,
            observed_base_sha=observed_base_sha,
            observed_base_tree_sha=observed_base_tree_sha,
            observed_at=observed_at,
        )
        return self.record_store.create_merge_landing_outcome_record_if_absent(outcome)[0]

    def record_provider_failure(
        self,
        *,
        admission: MergeAdmissionRecord,
        error: Exception,
        observed_at: str,
    ) -> MergeLandingOutcomeRecord:
        status_code = getattr(error, "status_code", None)
        conclusive_rejection = (
            isinstance(status_code, int)
            and 400 <= status_code < 500
            and (status_code not in {408, 429})
        )
        if conclusive_rejection:
            status = "rejected"
            reason: MergeLandingOutcomeReason = "provider_rejected"
        else:
            status = "reconcile_required"
            reason = "provider_transport_ambiguous"
        outcome = self._outcome(
            admission=admission,
            status=status,
            reason=reason,
            provider_effect_attempted=True,
            provider_conclusive_rejection=conclusive_rejection,
            provider_status_code=status_code if isinstance(status_code, int) else None,
            provider_message=str(error).strip(),
            observed_at=observed_at,
        )
        return self.record_store.create_merge_landing_outcome_record_if_absent(outcome)[0]

    def record_reconcile_required(
        self,
        *,
        admission: MergeAdmissionRecord,
        reason: MergeLandingOutcomeReason,
        message: str,
        observed_at: str,
    ) -> MergeLandingOutcomeRecord:
        outcome = self._outcome(
            admission=admission,
            status="reconcile_required",
            reason=reason,
            provider_effect_attempted=True,
            provider_message=message,
            observed_at=observed_at,
        )
        return self.record_store.create_merge_landing_outcome_record_if_absent(outcome)[0]

    def _outcome(
        self,
        *,
        admission: MergeAdmissionRecord,
        status: str,
        reason: MergeLandingOutcomeReason,
        provider_effect_attempted: bool,
        observed_at: str,
        provider_conclusive_rejection: bool = False,
        provider_status_code: int | None = None,
        provider_message: str = "",
        observed_pull_request_state: str = "",
        observed_pull_request_head_sha: str = "",
        observed_pull_request_head_tree_sha: str = "",
        observed_base_sha: str = "",
        observed_base_tree_sha: str = "",
        merge_commit_sha: str = "",
        merge_commit_tree_sha: str = "",
        base_contains_merge_commit: bool | None = None,
        exact_landing_confirmed: bool = False,
    ) -> MergeLandingOutcomeRecord:
        previous = self.record_store.list_merge_landing_outcome_records(
            admission_id=admission.admission_id,
            limit=1,
        )
        return MergeLandingOutcomeRecord.model_validate(
            {
                "admission_id": admission.admission_id,
                "admission_binding_sha256": admission.admission_binding_sha256,
                "attempt_id": admission.attempt_id,
                "observation_sequence": previous[0].observation_sequence + 1 if previous else 1,
                "prior_outcome_id": previous[0].outcome_id if previous else "",
                "source": f"service:merge-landing-outcome:{self.trace_id}",
                "repository": admission.repository,
                "base_branch": admission.base_branch,
                "pull_request_number": admission.pull_request_number,
                "status": status,
                "reason": reason,
                "provider_effect_attempted": provider_effect_attempted,
                "provider_conclusive_rejection": provider_conclusive_rejection,
                "provider_status_code": provider_status_code,
                "provider_message": provider_message,
                "observed_pull_request_state": observed_pull_request_state,
                "observed_pull_request_head_sha": observed_pull_request_head_sha,
                "observed_pull_request_head_tree_sha": observed_pull_request_head_tree_sha,
                "observed_base_sha": observed_base_sha,
                "observed_base_tree_sha": observed_base_tree_sha,
                "merge_commit_sha": merge_commit_sha,
                "merge_commit_tree_sha": merge_commit_tree_sha,
                "base_contains_merge_commit": base_contains_merge_commit,
                "exact_landing_confirmed": exact_landing_confirmed,
                "observed_at": observed_at,
            }
        )
