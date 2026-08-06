from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Protocol, cast

from control_plane.change_impact_service import (
    ChangeImpactRepositoryEvidenceProvider,
    evaluate_change_impact,
    load_change_impact_stored_evidence,
)
from control_plane.contracts.change_impact import (
    ChangeImpactEvaluation,
    ChangeImpactPolicyRecord,
)
from control_plane.contracts.engineering_review_decision import (
    EngineeringReviewDecisionRecord,
    EngineeringReviewDecisionRequest,
)
from control_plane.contracts.engineering_review_run import EngineeringReviewRunRecord
from control_plane.contracts.engineering_review_run import EngineeringReviewAuthorityRecord
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.workflows.launchplane import github_pull_request_reference


class EngineeringReviewDecisionConflictError(ValueError):
    pass


class EngineeringReviewDecisionStore(Protocol):
    def read_every_code_work_request_record(
        self, request_id: str
    ) -> EveryCodeWorkRequestRecord: ...

    def list_change_impact_policy_records(
        self,
        *,
        repository_id: str = "",
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ChangeImpactPolicyRecord, ...]: ...

    def list_engineering_review_run_records(
        self,
        *,
        repository: str = "",
        pr_number: int | None = None,
        head_sha: str = "",
        work_request_id: str = "",
        worker_runtime_id: str = "",
        worker_host: str = "",
        state: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EngineeringReviewRunRecord, ...]: ...

    def list_engineering_review_authority_records(
        self,
        *,
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EngineeringReviewAuthorityRecord, ...]: ...

    def write_engineering_review_decision_record_if_absent(
        self, record: EngineeringReviewDecisionRecord
    ) -> tuple[EngineeringReviewDecisionRecord, bool]: ...

    def read_engineering_review_decision_record(
        self, decision_id: str
    ) -> EngineeringReviewDecisionRecord: ...

    def list_engineering_review_decision_records(
        self,
        *,
        repository: str = "",
        pull_request_number: int | None = None,
        head_sha: str = "",
        work_request_id: str = "",
        limit: int | None = None,
    ) -> tuple[EngineeringReviewDecisionRecord, ...]: ...


def require_engineering_review_decision_store(
    record_store: object,
) -> EngineeringReviewDecisionStore:
    required = (
        "read_every_code_work_request_record",
        "list_change_impact_policy_records",
        "list_engineering_review_run_records",
        "list_engineering_review_authority_records",
        "write_engineering_review_decision_record_if_absent",
        "read_engineering_review_decision_record",
        "list_engineering_review_decision_records",
    )
    missing = [name for name in required if not callable(getattr(record_store, name, None))]
    if missing:
        raise TypeError(
            "record store does not support engineering review decisions: " + ", ".join(missing)
        )
    return cast(EngineeringReviewDecisionStore, record_store)


def evaluate_engineering_review_decision(
    *,
    store: EngineeringReviewDecisionStore,
    request: EngineeringReviewDecisionRequest,
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider,
    evaluated_at: str = "",
) -> tuple[EngineeringReviewDecisionRecord, bool]:
    work_request = store.read_every_code_work_request_record(request.work_request_id)
    if work_request.state != "done" or not work_request.result_pr_url:
        raise EngineeringReviewDecisionConflictError(
            "Engineering review decision requires a completed work request with a linked PR."
        )
    if work_request.repository != request.target.repository:
        raise EngineeringReviewDecisionConflictError(
            "Engineering review target does not match the stored work request repository."
        )
    reference = github_pull_request_reference(pr_url=work_request.result_pr_url)
    reference_repository = (
        ""
        if reference is None
        else f"{reference['owner']}/{reference['repo']}".casefold()
    )
    if (
        reference is None
        or reference_repository != work_request.repository.casefold()
        or reference_repository != request.target.repository.casefold()
        or int(reference["pr_number"]) != request.target.pull_request_number
    ):
        raise EngineeringReviewDecisionConflictError(
            "Engineering review target does not match the stored work request pull request."
        )
    repository_evidence = repository_evidence_provider.resolve(request.target)
    target = repository_evidence.target
    policies = store.list_change_impact_policy_records(repository_id=target.repository_id)
    stored_evidence = load_change_impact_stored_evidence(store=store, target=target)
    impact = evaluate_change_impact(
        repository_evidence=repository_evidence,
        policies=policies,
        stored_evidence=stored_evidence,
        evaluated_at=evaluated_at,
    )
    runs = store.list_engineering_review_run_records(
        repository=target.repository,
        pr_number=target.pull_request_number,
        head_sha=target.head_sha,
        work_request_id=work_request.request_id,
        limit=50,
    )
    active_authorities = store.list_engineering_review_authority_records(
        repository=target.repository,
        status="active",
        limit=2,
    )
    record = _decision_record(
        work_request=work_request,
        impact=impact,
        runs=runs,
        active_authority=(active_authorities[0] if len(active_authorities) == 1 else None),
        evaluated_at=evaluated_at.strip() or _decision_timestamp(),
    )
    return store.write_engineering_review_decision_record_if_absent(record)


def _decision_record(
    *,
    work_request: EveryCodeWorkRequestRecord,
    impact: ChangeImpactEvaluation,
    runs: tuple[EngineeringReviewRunRecord, ...],
    active_authority: EngineeringReviewAuthorityRecord | None,
    evaluated_at: str,
) -> EngineeringReviewDecisionRecord:
    target = impact.target
    exact_runs = tuple(
        run
        for run in runs
        if run.repository == target.repository
        and run.pr_number == target.pull_request_number
        and run.head_sha == target.head_sha
        and run.tree_sha == target.tree_sha
        and run.work_request_id == work_request.request_id
        and run.work_request_lifecycle_id == work_request.lifecycle_id
    )
    authority_runs = tuple(
        run
        for run in exact_runs
        if active_authority is not None
        and run.authority_id == active_authority.authority_id
        and run.authority_digest == active_authority.authority_digest
        and run.policy_revision == active_authority.policy_revision
    )
    completed = tuple(run for run in authority_runs if run.state == "completed")
    approved = tuple(run for run in completed if run.decision == "approved")
    requested = tuple(run for run in completed if run.decision == "changes_requested")
    blocked = tuple(run for run in completed if run.decision == "blocked")
    required = impact.required_engineering_review_count
    qualifying = _distinct_slots(approved)
    families = tuple(sorted({run.model_family for run in qualifying}))

    status: Literal["approved", "changes_requested", "blocked", "pending", "unknown", "stale"]
    reason_code: str
    if impact.status in {"stale_head", "stale_policy"}:
        status, reason_code = "stale", f"change_impact_{impact.status}"
    elif impact.status != "success":
        status, reason_code = "unknown", "change_impact_unknown"
    elif active_authority is None:
        status, reason_code = "unknown", "review_authority_unavailable"
    elif exact_runs and not authority_runs:
        status, reason_code = "stale", "review_authority_stale"
    elif blocked:
        status, reason_code = "blocked", "review_blocked"
    elif requested:
        status, reason_code = "changes_requested", "changes_requested"
    elif len(qualifying) < required:
        status, reason_code = "pending", "insufficient_completed_reviews"
    elif required == 2 and len(families) < 2:
        status, reason_code = "unknown", "insufficient_model_family_diversity"
    else:
        status, reason_code = "approved", "required_reviews_approved"

    authority_id = active_authority.authority_id if active_authority is not None else ""
    authority_digest = (
        active_authority.authority_digest if active_authority is not None else ""
    )
    authority_revision = (
        active_authority.policy_revision if active_authority is not None else None
    )
    return EngineeringReviewDecisionRecord(
        status=status,
        reason_code=reason_code,
        target=target,
        work_request_id=work_request.request_id,
        work_request_lifecycle_id=work_request.lifecycle_id,
        change_impact_status=impact.status,
        change_impact_policy_record_id=impact.policy_record_id,
        change_impact_policy_revision=impact.policy_revision,
        change_impact_policy_digest=impact.policy_digest,
        engineering_review_tier=impact.engineering_review_tier,
        required_review_count=required,
        authority_id=authority_id,
        authority_digest=authority_digest,
        authority_policy_revision=authority_revision,
        qualifying_run_ids=tuple(sorted(run.run_id for run in qualifying)),
        qualifying_model_families=families,
        evaluated_at=evaluated_at,
    )


def _distinct_slots(
    runs: tuple[EngineeringReviewRunRecord, ...],
) -> tuple[EngineeringReviewRunRecord, ...]:
    selected: dict[int, EngineeringReviewRunRecord] = {}
    for run in sorted(runs, key=lambda candidate: (candidate.finished_at, candidate.run_id)):
        selected.setdefault(run.review_slot, run)
    return tuple(selected[slot] for slot in sorted(selected))


def _decision_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
