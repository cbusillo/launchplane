from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json

from control_plane.change_impact_github import (
    ChangeImpactRepositoryEvidenceError,
    ChangeImpactRepositoryEvidenceStaleError,
)
from control_plane.change_impact_service import (
    ChangeImpactRepositoryEvidenceProvider,
    evaluate_change_impact,
    load_change_impact_stored_evidence,
    require_change_impact_policy_read_store,
)
from control_plane.contracts.change_impact import (
    ChangeImpactEvaluation,
    ChangeImpactRepositoryEvidence,
    ChangeImpactTargetReference,
)
from control_plane.contracts.merge_readiness import (
    MERGE_READINESS_POLICY_DIMENSIONS,
    MergeReadinessPolicyFingerprintEvidence,
    MergeReadinessPolicyFingerprints,
    MergeReadinessTarget,
)
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainCombinedCandidateOwnerReview,
    MergeTrainOwnerEvidenceBinding,
    MergeTrainStructuralDeltaFingerprint,
    MergeTrainStructuralEntryObservation,
    MergeTrainStructuralEvaluationInput,
    MergeTrainStructuralSubject,
)
from control_plane.contracts.owner_acceptance import OwnerAcceptanceDecision
from control_plane.durable_operation_authorization import read_active_authz_policy_record
from control_plane.engineering_review_decision_service import (
    require_engineering_review_decision_store,
)
from control_plane.merge_admission import (
    MERGE_ADMISSION_ALGORITHM_VERSION,
    MergeAdmissionDeniedError,
    MergeAdmissionEvaluation,
)
from control_plane.merge_admission_impact_binding import (
    impact_binding_fingerprints,
    select_current_engineering_decision,
)
from control_plane.merge_readiness import evaluate_merge_readiness_from_live_evidence
from control_plane.merge_train import (
    MergeTrainSnapshotReader,
    build_merge_train_dry_run_result,
)
from control_plane.merge_train_github import GitHubMergeTrainSnapshotReader
from control_plane.merge_train_policy_source import (
    MergeTrainPolicyStoreMissingError,
    resolve_merge_train_policy_record,
)
from control_plane.merge_train_structural_provenance import (
    evaluate_merge_train_structural_candidate,
)
from control_plane.owner_acceptance import (
    OwnerAcceptanceEvaluationUnavailableError,
    evaluate_owner_acceptance,
)
from control_plane.tenant_admission_controller import (
    TenantAdmissionControllerGitHubClient,
    TenantAdmissionTechnicalChecks,
)


_MISSING_POLICY_SHA256 = hashlib.sha256(b"launchplane-policy-unavailable").hexdigest()
MERGE_ADMISSION_ALGORITHM_SHA256 = hashlib.sha256(
    MERGE_ADMISSION_ALGORITHM_VERSION.encode("utf-8")
).hexdigest()


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class _EntryEvidence:
    repository_evidence: ChangeImpactRepositoryEvidence
    impact: ChangeImpactEvaluation
    owner_decision: OwnerAcceptanceDecision
    observation: MergeTrainStructuralEntryObservation


@dataclass(frozen=True)
class LiveMergeAdmissionEvaluator:
    store: object
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider
    technical_check_client: TenantAdmissionControllerGitHubClient
    policy_record_provider: Callable[[], MergeTrainPolicyRecord] | None = None
    snapshot_reader: MergeTrainSnapshotReader | None = None

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
        landing_plan = landing_plan_record.landing_plan
        snapshot_reader = self.snapshot_reader or GitHubMergeTrainSnapshotReader(
            transport=self.technical_check_client.transport
        )
        try:
            policy_record = (
                self.policy_record_provider()
                if self.policy_record_provider is not None
                else resolve_merge_train_policy_record(self.store)
            )
            snapshot = snapshot_reader.read_merge_train_snapshot(
                repository=landing_plan.repository,
                base_branch=landing_plan.base_branch,
            )
            live_queue = build_merge_train_dry_run_result(
                policy=policy_record.policy,
                snapshot=snapshot,
            )
            repository_policy = policy_record.policy.find_repository_policy(
                repository=landing_plan.repository,
                base_branch=landing_plan.base_branch,
            )
        except (LookupError, ValueError, MergeTrainPolicyStoreMissingError) as error:
            raise MergeAdmissionDeniedError(
                "Active merge-train policy no longer admits the landing-plan lineage.",
                reason_code="landing_policy_not_admitted",
            ) from error
        expected_queue = tuple(
            (plan_entry.pull_request_number, plan_entry.expected_head_sha)
            for plan_entry in landing_plan.entries
            if plan_entry.status not in {"merged", "skipped"}
        )
        live_queue_by_number = {queue_entry.number: queue_entry for queue_entry in live_queue.queue}
        live_queue_identity = tuple(
            (pull_request_number, live_queue_by_number[pull_request_number].head_sha)
            for pull_request_number in live_queue.queue_order
        )
        if snapshot.base_sha != observed_base_sha or live_queue_identity != expected_queue:
            raise MergeAdmissionDeniedError(
                "Live merge queue or base identity changed from the landing-plan lineage.",
                reason_code="landing_lineage_changed",
            )
        try:
            entry_evidence = tuple(
                self._entry_evidence(
                    repository=landing_plan.repository,
                    pull_request_number=candidate_entry.pull_request_number,
                    evaluated_at=evaluated_at,
                    position=candidate_entry.position,
                )
                for candidate_entry in candidate_record.candidate.entries
            )
        except (
            ChangeImpactRepositoryEvidenceError,
            OwnerAcceptanceEvaluationUnavailableError,
        ) as error:
            evidence_error = (
                error.__cause__
                if isinstance(error, OwnerAcceptanceEvaluationUnavailableError)
                else error
            )
            if not isinstance(evidence_error, ChangeImpactRepositoryEvidenceError):
                raise
            if isinstance(evidence_error, ChangeImpactRepositoryEvidenceStaleError):
                message = "Authoritative repository evidence changed during merge admission."
                reason_code = "repository_evidence_stale"
            else:
                message = "Authoritative repository evidence is unavailable for merge admission."
                reason_code = "repository_evidence_unavailable"
            raise MergeAdmissionDeniedError(message, reason_code=reason_code) from error
        observations = tuple(item.observation for item in entry_evidence)
        combined_owner_review = self._combined_owner_review(
            landing_plan_record=landing_plan_record,
            entry_evidence=entry_evidence,
        )
        structural_result = evaluate_merge_train_structural_candidate(
            evaluation=MergeTrainStructuralEvaluationInput(
                repository=landing_plan.repository,
                base_branch=landing_plan.base_branch,
                target_pull_request_number=entry.pull_request_number,
                target_queue_position=entry.position,
                observed_base_sha=observed_base_sha,
                observed_base_tree_sha=observed_base_tree_sha,
                policy_key=landing_plan.policy_key,
                policy_sha256=landing_plan.policy_sha256,
                active_candidate_sha256=landing_plan.candidate_sha256,
                active_landing_plan_sha256=landing_plan.landing_plan_sha256,
                entries=observations,
                combined_owner_review=combined_owner_review,
            ),
            candidate_record=candidate_record,
            landing_plan_record=landing_plan_record,
            stack_collapse_record=stack_collapse_record,
        )
        target_evidence = next(
            item
            for item in entry_evidence
            if item.repository_evidence.target.pull_request_number == entry.pull_request_number
        )
        engineering_store = require_engineering_review_decision_store(self.store)
        decisions = engineering_store.list_engineering_review_decision_records(
            repository=landing_plan.repository,
            pull_request_number=entry.pull_request_number,
            head_sha=observed_head_sha,
            limit=25,
        )
        engineering_decision = select_current_engineering_decision(
            impact=target_evidence.impact, decisions=decisions
        )
        engineering_runs = (
            engineering_store.list_engineering_review_run_records(
                repository=landing_plan.repository,
                pr_number=entry.pull_request_number,
                head_sha=observed_head_sha,
                work_request_id=engineering_decision.work_request_id,
                limit=50,
            )
            if engineering_decision is not None
            else ()
        )
        authorities = engineering_store.list_engineering_review_authority_records(
            repository=landing_plan.repository,
            status="active",
            limit=2,
        )
        active_authority = authorities[0] if len(authorities) == 1 else None
        technical_checks = self.technical_check_client.read_technical_checks(
            repository=landing_plan.repository,
            base_branch=landing_plan.base_branch,
            base_sha=observed_base_sha,
            head_sha=landing_plan.candidate_sha,
            evaluated_at=evaluated_at,
        )
        policy_fingerprints = self._policy_fingerprints(
            impact=target_evidence.impact,
            owner_decision=target_evidence.owner_decision,
            engineering_decision=engineering_decision,
            engineering_authority_digest=(
                active_authority.authority_digest if active_authority is not None else None
            ),
            technical_checks=technical_checks,
            landing_policy_sha256=landing_plan.policy_sha256,
            current_merge_train_policy_sha256=policy_record.policy_sha256,
        )
        readiness = evaluate_merge_readiness_from_live_evidence(
            target=MergeReadinessTarget(
                repository=landing_plan.repository,
                pull_request_number=entry.pull_request_number,
                base_branch=landing_plan.base_branch,
                base_sha=observed_base_sha,
                pull_request_head_sha=observed_head_sha,
                pull_request_tree_sha=observed_head_tree_sha,
                expected_effect_sha=landing_plan.candidate_sha,
                queue_position=entry.position,
            ),
            owner_decision=target_evidence.owner_decision,
            engineering_decision=engineering_decision,
            engineering_runs=engineering_runs,
            engineering_review_authority=repository_policy.engineering_review_mode,
            technical_checks=technical_checks,
            policy_fingerprints=policy_fingerprints,
            candidate_record=candidate_record,
            structural_candidate_status=structural_result.status,
            controller_state=controller_state,
            expected_lease_owner=expected_lease_owner,
            observed_effect_sha=landing_plan.candidate_sha,
            evaluated_at=evaluated_at,
        )
        return MergeAdmissionEvaluation(
            readiness=readiness,
            structural_result=structural_result,
        )

    def _entry_evidence(
        self,
        *,
        repository: str,
        pull_request_number: int,
        evaluated_at: str,
        position: int,
    ) -> _EntryEvidence:
        target_reference = ChangeImpactTargetReference(
            repository=repository,
            pull_request_number=pull_request_number,
        )
        repository_evidence = self.repository_evidence_provider.resolve(target_reference)
        policy_store = require_change_impact_policy_read_store(self.store)
        impact = evaluate_change_impact(
            repository_evidence=repository_evidence,
            policies=policy_store.list_change_impact_policy_records(
                repository_id=repository_evidence.target.repository_id,
            ),
            stored_evidence=load_change_impact_stored_evidence(
                store=self.store,
                target=repository_evidence.target,
            ),
            evaluated_at=evaluated_at,
        )
        owner_decision = evaluate_owner_acceptance(
            store=self.store,
            repository_evidence_provider=self.repository_evidence_provider,
            target=target_reference,
            evaluated_at=evaluated_at,
        )
        current_delta = self._current_delta(
            repository_evidence=repository_evidence,
            impact=impact,
        )
        reviewed_delta = (
            current_delta
            if self._owner_evidence_matches_current(
                decision=owner_decision,
                repository_evidence=repository_evidence,
            )
            else None
        )
        observation = MergeTrainStructuralEntryObservation(
            position=position,
            pull_request_number=pull_request_number,
            head_sha=repository_evidence.target.head_sha,
            head_tree_sha=repository_evidence.target.tree_sha,
            reviewed_delta=reviewed_delta,
            current_delta=current_delta,
        )
        return _EntryEvidence(
            repository_evidence=repository_evidence,
            impact=impact,
            owner_decision=owner_decision,
            observation=observation,
        )

    def _current_delta(
        self,
        *,
        repository_evidence: ChangeImpactRepositoryEvidence,
        impact: ChangeImpactEvaluation,
    ) -> MergeTrainStructuralDeltaFingerprint | None:
        if impact.status != "success":
            return None
        return MergeTrainStructuralDeltaFingerprint(
            head_sha=repository_evidence.target.head_sha,
            head_tree_sha=repository_evidence.target.tree_sha,
            changed_paths=tuple(item.path for item in repository_evidence.changed_files),
            affected_subjects=tuple(
                MergeTrainStructuralSubject(product=item.product, system=item.system)
                for item in impact.affected_products
            ),
        )

    def _owner_evidence_matches_current(
        self,
        *,
        decision: OwnerAcceptanceDecision,
        repository_evidence: ChangeImpactRepositoryEvidence,
    ) -> bool:
        if decision.status == "not_required":
            return True
        if decision.status != "accepted" or not decision.admissible or not decision.products:
            return False
        return all(
            product.status == "accepted"
            and product.admissible
            and product.binding is not None
            and product.current_event is not None
            and product.binding.head_sha == repository_evidence.target.head_sha
            and product.binding.tree_sha == repository_evidence.target.tree_sha
            for product in decision.products
        )

    def _combined_owner_review(
        self,
        *,
        landing_plan_record: MergeTrainBatchLandingPlanRecord,
        entry_evidence: tuple[_EntryEvidence, ...],
    ) -> MergeTrainCombinedCandidateOwnerReview | None:
        observations = tuple(item.observation for item in entry_evidence)
        if any(
            observation.reviewed_delta is None or observation.current_delta is None
            for observation in observations
        ):
            return None
        bindings = {
            (product.current_event.event_id, product.binding.binding_sha256)
            for item in entry_evidence
            for product in item.owner_decision.products
            if product.status == "accepted"
            and product.admissible
            and product.current_event is not None
            and product.binding is not None
        }
        if not bindings:
            return None
        landing_plan = landing_plan_record.landing_plan
        return MergeTrainCombinedCandidateOwnerReview(
            evidence_bindings=tuple(
                MergeTrainOwnerEvidenceBinding(
                    event_id=event_id,
                    binding_sha256=binding_sha256,
                )
                for event_id, binding_sha256 in sorted(bindings)
            ),
            candidate_sha256=landing_plan.candidate_sha256,
            landing_plan_sha256=landing_plan.landing_plan_sha256,
            policy_key=landing_plan.policy_key,
            policy_sha256=landing_plan.policy_sha256,
            entries=observations,
        )

    def _policy_fingerprints(
        self,
        *,
        impact: ChangeImpactEvaluation,
        owner_decision: OwnerAcceptanceDecision,
        engineering_decision: object | None,
        engineering_authority_digest: str | None,
        technical_checks: TenantAdmissionTechnicalChecks,
        landing_policy_sha256: str,
        current_merge_train_policy_sha256: str,
    ) -> MergeReadinessPolicyFingerprints:
        technical_policy_sha256 = _canonical_sha256(
            {
                "strict": technical_checks.strict,
                "required_checks": [
                    check.model_dump(mode="json") for check in technical_checks.required_checks
                ],
                "excluded_contexts": list(technical_checks.excluded_contexts),
            }
        )
        expected_impact, current_impact = impact_binding_fingerprints(
            impact=impact,
            owner_decision=owner_decision,
            engineering_decision=engineering_decision,
        )
        expected_engineering = (
            str(getattr(engineering_decision, "authority_digest", "")).strip()
            or _MISSING_POLICY_SHA256
        )
        try:
            authorization_sha256 = read_active_authz_policy_record(self.store).policy_sha256
        except (LookupError, TypeError, ValueError):
            authorization_sha256 = None

        values: dict[str, tuple[str, str | None]] = {
            "impact": (expected_impact, current_impact),
            "technical_checks": (technical_policy_sha256, technical_policy_sha256),
            "engineering_review": (expected_engineering, engineering_authority_digest),
            "ruleset": (technical_policy_sha256, technical_policy_sha256),
            "merge_train": (
                landing_policy_sha256,
                current_merge_train_policy_sha256,
            ),
            "authorization": (
                authorization_sha256 or _MISSING_POLICY_SHA256,
                authorization_sha256,
            ),
            "admission_algorithm": (
                MERGE_ADMISSION_ALGORITHM_SHA256,
                MERGE_ADMISSION_ALGORITHM_SHA256,
            ),
        }
        return MergeReadinessPolicyFingerprints.model_validate(
            {
                dimension: MergeReadinessPolicyFingerprintEvidence(
                    dimension=dimension,
                    expected_sha256=values[dimension][0],
                    current_sha256=values[dimension][1],
                )
                for dimension in MERGE_READINESS_POLICY_DIMENSIONS
            }
        )
