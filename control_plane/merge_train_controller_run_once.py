from dataclasses import dataclass
import logging

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_candidate,
    build_merge_train_batch_candidate_record,
    build_merge_train_batch_landing_plan,
    build_merge_train_batch_landing_plan_record,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicy, MergeTrainRepositoryPolicy
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
    build_merge_train_stack_collapse_plan,
    build_merge_train_stack_collapse_plan_record,
    execute_merge_train_stack_collapse_plan,
    reconcile_merge_train_stack_children_after_root_landing,
)
from control_plane.merge_train import (
    MergeTrainDryRunResult,
    build_merge_train_dry_run_result,
    discover_merge_train_stack,
)
from control_plane.merge_train_batch_candidate import (
    MergeTrainBatchCandidateRecordStore,
    merge_train_snapshot_has_stack_topology,
)
from control_plane.merge_train_batch_landing import (
    MergeTrainBatchLandingPlanRecordStore,
    validate_stack_collapse_record_for_landing,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    MergeTrainGitHubError,
    MergeTrainGitHubStaleHeadError,
    MergeTrainGitHubTransport,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecordStore,
    stack_collapse_expected_root_head_sha,
)
from control_plane.workflows.merge_train_controller import (
    latest_completed_merge_train_batch_landing_plan_record as latest_completed_merge_train_batch_landing_progress_record,
    latest_merge_train_batch_candidate_progress_record,
    latest_merge_train_batch_landing_progress_record,
    latest_merge_train_stack_collapse_progress_record,
)


_LOGGER = logging.getLogger(__name__)


class MergeTrainControllerRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    mutate: bool = False
    github_api_base_url: str = "https://api.github.com"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainControllerRunOnceEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        self.github_api_base_url = self.github_api_base_url.strip() or "https://api.github.com"
        if not self.repository:
            raise ValueError("merge train controller requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train controller requires base_branch")
        return self


class MergeTrainControllerRequestError(ValueError):
    pass


@dataclass(frozen=True)
class MergeTrainControllerRunOnceResult:
    accepted_result: dict[str, object]
    records: dict[str, str]


def execute_merge_train_controller_run_once(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    token: str,
    trace_id: str,
    recorded_at: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
) -> MergeTrainControllerRunOnceResult:
    transport = UrllibMergeTrainGitHubTransport(
        token=token,
        api_base_url=request.github_api_base_url,
    )
    github_client = GitHubMergeTrainClient(transport=transport)
    active_landing_record = latest_merge_train_batch_landing_plan_record(
        record_store=landing_store,
        repository=request.repository,
        base_branch=request.base_branch,
    )
    if active_landing_record is not None:
        result = _advance_active_landing_record(
            request=request,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            trace_id=trace_id,
            recorded_at=recorded_at,
            github_client=github_client,
            landing_store=landing_store,
            stack_collapse_store=stack_collapse_store,
            active_landing_record=active_landing_record,
        )
        return MergeTrainControllerRunOnceResult(
            accepted_result=result,
            records=_records_for_result(result),
        )

    result = _advance_without_active_landing(
        request=request,
        policy=policy,
        policy_sha256=policy_sha256,
        repository_policy=repository_policy,
        transport=transport,
        github_client=github_client,
        trace_id=trace_id,
        recorded_at=recorded_at,
        candidate_store=candidate_store,
        landing_store=landing_store,
        stack_collapse_store=stack_collapse_store,
    )
    return MergeTrainControllerRunOnceResult(
        accepted_result=result,
        records=_records_for_result(result),
    )


def latest_merge_train_batch_candidate_record(
    *,
    record_store: MergeTrainBatchCandidateRecordStore,
    repository: str,
    base_branch: str,
) -> MergeTrainBatchCandidateRecord | None:
    records = record_store.list_merge_train_batch_candidate_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    latest_record = latest_merge_train_batch_candidate_progress_record(records)
    if latest_record is None:
        return None
    if latest_record.candidate.status in {"passed", "stale", "blocked"}:
        return None
    return latest_record


def latest_passed_merge_train_batch_candidate_record(
    *,
    record_store: MergeTrainBatchCandidateRecordStore,
    landing_plan_record_store: MergeTrainBatchLandingPlanRecordStore,
    repository: str,
    base_branch: str,
) -> MergeTrainBatchCandidateRecord | None:
    records = record_store.list_merge_train_batch_candidate_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    latest_record = latest_merge_train_batch_candidate_progress_record(records)
    if latest_record is None or latest_record.candidate.status != "passed":
        return None
    completed_landing_record = latest_completed_merge_train_batch_landing_plan_record(
        record_store=landing_plan_record_store,
        repository=repository,
        base_branch=base_branch,
        batch_id=latest_record.candidate.batch_id,
        candidate_sha=latest_record.candidate.candidate_sha,
    )
    if completed_landing_record is not None:
        return None
    return latest_record


def latest_merge_train_batch_landing_plan_record(
    *,
    record_store: MergeTrainBatchLandingPlanRecordStore,
    repository: str,
    base_branch: str,
) -> MergeTrainBatchLandingPlanRecord | None:
    records = record_store.list_merge_train_batch_landing_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    latest_record = latest_merge_train_batch_landing_progress_record(records)
    if latest_record is None:
        return None
    if not any(entry.status == "planned" for entry in latest_record.landing_plan.entries):
        return None
    return latest_record


def latest_completed_merge_train_batch_landing_plan_record(
    *,
    record_store: MergeTrainBatchLandingPlanRecordStore,
    repository: str,
    base_branch: str,
    batch_id: str,
    candidate_sha: str,
) -> MergeTrainBatchLandingPlanRecord | None:
    records = record_store.list_merge_train_batch_landing_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    return latest_completed_merge_train_batch_landing_progress_record(
        landing_plan_records=records,
        batch_id=batch_id,
        candidate_sha=candidate_sha,
    )


def latest_merge_train_stack_collapse_plan_record(
    *,
    record_store: MergeTrainStackCollapsePlanRecordStore,
    repository: str,
    base_branch: str,
    plan_status: str,
) -> MergeTrainStackCollapsePlanRecord | None:
    records = record_store.list_merge_train_stack_collapse_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    latest_record = latest_merge_train_stack_collapse_progress_record(records)
    if latest_record is None or latest_record.plan.status != plan_status:
        return None
    return latest_record


def latest_merge_train_stack_collapse_plan_record_for_landing(
    *,
    record_store: MergeTrainStackCollapsePlanRecordStore,
    repository: str,
    base_branch: str,
    landing_plan: MergeTrainBatchLandingPlan,
    policy_sha256: str,
) -> MergeTrainStackCollapsePlanRecord | None:
    records = record_store.list_merge_train_stack_collapse_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    compatible_records = tuple(
        record
        for record in records
        if _merge_train_stack_collapse_record_matches_landing_plan(
            collapse_record=record,
            landing_plan=landing_plan,
            policy_sha256=policy_sha256,
        )
    )
    return latest_merge_train_stack_collapse_progress_record(compatible_records)


def _advance_active_landing_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    trace_id: str,
    recorded_at: str,
    github_client: GitHubMergeTrainClient,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    active_landing_record: MergeTrainBatchLandingPlanRecord,
) -> dict[str, object]:
    try:
        validate_merge_train_landing_record_for_controller(
            landing_record=active_landing_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    collapse_record = latest_merge_train_stack_collapse_plan_record_for_landing(
        record_store=stack_collapse_store,
        repository=request.repository,
        base_branch=request.base_branch,
        landing_plan=active_landing_record.landing_plan,
        policy_sha256=policy_sha256,
    )
    if collapse_record is not None:
        try:
            validate_stack_collapse_record_for_landing(
                collapse_record=collapse_record,
                landing_plan=active_landing_record.landing_plan,
                policy_sha256=policy_sha256,
            )
        except ValueError as error:
            raise MergeTrainControllerRequestError(str(error)) from error
        if not repository_policy.stack_child_disposition_label:
            raise ValueError(
                "merge train stack child disposition requires stack_child_disposition_label policy"
            )
    if not request.mutate:
        result: dict[str, object] = {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "land_batch",
            "merge_train_batch_landing_plan_record_id": active_landing_record.record_id,
        }
        if collapse_record is not None:
            result["merge_train_stack_collapse_plan_record_id"] = collapse_record.record_id
        return result

    try:
        landed_plan = github_client.land_batch_candidate(
            landing_plan=active_landing_record.landing_plan
        )
    except MergeTrainGitHubStaleHeadError as error:
        stale_plan = stale_merge_train_landing_plan(active_landing_record.landing_plan)
        stale_record = build_merge_train_batch_landing_plan_record(
            landing_plan=stale_plan,
            source=f"service:controller:stale-landing:{trace_id}",
            updated_at=recorded_at,
        )
        landing_store.write_merge_train_batch_landing_plan_record(stale_record)
        message = str(error).strip() or "Merge train landing evidence no longer matches GitHub."
        return {
            "merge_train_batch_landing_plan_record_id": stale_record.record_id,
            "repository": stale_plan.repository,
            "base_branch": stale_plan.base_branch,
            "mode": "stale_landing",
            "controller_action": "land_batch",
            "landing_plan": stale_plan.model_dump(mode="json"),
            "error": {
                "code": "merge_train_github_stale_state",
                "message": message,
            },
            "details": {
                "github_status_code": error.status_code,
            },
        }

    landed_record = build_merge_train_batch_landing_plan_record(
        landing_plan=landed_plan,
        source=f"service:controller:land:{trace_id}",
        updated_at=recorded_at,
    )
    landing_store.write_merge_train_batch_landing_plan_record(landed_record)
    candidate_ref_cleanup_result = cleanup_merge_train_batch_candidate_ref(
        github_client=github_client,
        landing_plan=landed_plan,
        trace_id=trace_id,
    )
    result = {
        "merge_train_batch_landing_plan_record_id": landed_record.record_id,
        "repository": landed_plan.repository,
        "base_branch": landed_plan.base_branch,
        "mode": "land",
        "controller_action": "land_batch",
        "landing_plan": landed_plan.model_dump(mode="json"),
        **candidate_ref_cleanup_result,
    }
    if collapse_record is None:
        return result

    root_entry = next(
        (
            entry
            for entry in landed_plan.entries
            if entry.pull_request_number == collapse_record.plan.root_pull_request_number
        ),
        None,
    )
    if root_entry is None or root_entry.status != "merged":
        raise ValueError("merge train stack child disposition requires merged root PR")
    reconciled_collapse_plan = reconcile_merge_train_stack_children_after_root_landing(
        plan=collapse_record.plan,
        disposition_client=github_client,
        root_merge_commit_sha=root_entry.merge_commit_sha,
        label=repository_policy.stack_child_disposition_label,
        updated_at=recorded_at,
    )
    reconciled_record = build_merge_train_stack_collapse_plan_record(
        plan=reconciled_collapse_plan,
        source=f"service:controller:child-disposition:{trace_id}",
        updated_at=recorded_at,
    )
    stack_collapse_store.write_merge_train_stack_collapse_plan_record(reconciled_record)
    result["merge_train_stack_collapse_plan_record_id"] = reconciled_record.record_id
    result["stack_collapse_plan"] = reconciled_collapse_plan.model_dump(mode="json")
    return result


def _advance_without_active_landing(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    transport: MergeTrainGitHubTransport,
    github_client: GitHubMergeTrainClient,
    trace_id: str,
    recorded_at: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
) -> dict[str, object]:
    active_candidate_record = latest_merge_train_batch_candidate_record(
        record_store=candidate_store,
        repository=request.repository,
        base_branch=request.base_branch,
    )
    passed_candidate_record = None
    if active_candidate_record is None:
        passed_candidate_record = latest_passed_merge_train_batch_candidate_record(
            record_store=candidate_store,
            landing_plan_record_store=landing_store,
            repository=request.repository,
            base_branch=request.base_branch,
        )
    if active_candidate_record is not None:
        return _advance_active_candidate_record(
            request=request,
            policy=policy,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            transport=transport,
            github_client=github_client,
            trace_id=trace_id,
            recorded_at=recorded_at,
            candidate_store=candidate_store,
            active_candidate_record=active_candidate_record,
        )
    if passed_candidate_record is not None:
        return _advance_passed_candidate_record(
            request=request,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            trace_id=trace_id,
            recorded_at=recorded_at,
            landing_store=landing_store,
            passed_candidate_record=passed_candidate_record,
        )
    return _advance_without_candidate_record(
        request=request,
        policy=policy,
        policy_sha256=policy_sha256,
        repository_policy=repository_policy,
        transport=transport,
        github_client=github_client,
        trace_id=trace_id,
        recorded_at=recorded_at,
        candidate_store=candidate_store,
        stack_collapse_store=stack_collapse_store,
    )


def _advance_active_candidate_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    transport: MergeTrainGitHubTransport,
    github_client: GitHubMergeTrainClient,
    trace_id: str,
    recorded_at: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    active_candidate_record: MergeTrainBatchCandidateRecord,
) -> dict[str, object]:
    try:
        validate_merge_train_candidate_record_for_controller(
            candidate_record=active_candidate_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    if active_candidate_record.candidate.status == "failed":
        reflow_result = try_reflow_failed_merge_train_candidate(
            candidate_store=candidate_store,
            active_candidate_record=active_candidate_record,
            policy=policy,
            policy_sha256=policy_sha256,
            transport=transport,
            repository=request.repository,
            base_branch=request.base_branch,
            recorded_at=recorded_at,
            trace_id=trace_id,
            mutate=request.mutate,
        )
        if reflow_result is not None:
            return reflow_result
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "candidate_failed",
            "merge_train_batch_candidate_record_id": active_candidate_record.record_id,
            "candidate": active_candidate_record.candidate.model_dump(mode="json"),
        }

    if active_candidate_record.candidate.status in {"planned", "building"}:
        controller_action = "build_candidate"
        candidate = (
            github_client.build_batch_candidate(candidate=active_candidate_record.candidate)
            if request.mutate
            else active_candidate_record.candidate
        )
    else:
        controller_action = "observe_candidate"
        candidate = (
            github_client.observe_batch_candidate_checks(
                candidate=active_candidate_record.candidate
            )
            if request.mutate
            else active_candidate_record.candidate
        )
    result: dict[str, object] = {
        "repository": request.repository,
        "base_branch": request.base_branch,
        "mode": "dry-run" if not request.mutate else controller_action,
        "controller_action": controller_action,
        "merge_train_batch_candidate_record_id": active_candidate_record.record_id,
    }
    if request.mutate:
        updated_candidate_record = build_merge_train_batch_candidate_record(
            candidate=candidate,
            source=f"service:controller:{controller_action}:{trace_id}",
            updated_at=recorded_at,
        )
        candidate_store.write_merge_train_batch_candidate_record(updated_candidate_record)
        result["merge_train_batch_candidate_record_id"] = updated_candidate_record.record_id
    result["candidate"] = candidate.model_dump(mode="json")
    return result


def _advance_passed_candidate_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    trace_id: str,
    recorded_at: str,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    passed_candidate_record: MergeTrainBatchCandidateRecord,
) -> dict[str, object]:
    try:
        validate_merge_train_candidate_record_for_controller(
            candidate_record=passed_candidate_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    completed_landing_record = latest_completed_merge_train_batch_landing_plan_record(
        record_store=landing_store,
        repository=request.repository,
        base_branch=request.base_branch,
        batch_id=passed_candidate_record.candidate.batch_id,
        candidate_sha=passed_candidate_record.candidate.candidate_sha,
    )
    if completed_landing_record is not None:
        try:
            validate_merge_train_landing_record_for_controller(
                landing_record=completed_landing_record,
                policy_key=repository_policy.policy_key,
                policy_sha256=policy_sha256,
            )
        except ValueError as error:
            raise MergeTrainControllerRequestError(str(error)) from error
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "batch_landed",
            "merge_train_batch_candidate_record_id": passed_candidate_record.record_id,
            "merge_train_batch_landing_plan_record_id": completed_landing_record.record_id,
            "landing_plan": completed_landing_record.landing_plan.model_dump(mode="json"),
        }
    if not request.mutate:
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "plan_landing",
            "merge_train_batch_candidate_record_id": passed_candidate_record.record_id,
        }

    landing_plan = build_merge_train_batch_landing_plan(
        candidate=passed_candidate_record.candidate,
        merge_method=repository_policy.merge_method,
        created_at=recorded_at,
    )
    landing_record = build_merge_train_batch_landing_plan_record(
        landing_plan=landing_plan,
        source=f"service:controller:landing-plan:{trace_id}",
        updated_at=recorded_at,
    )
    landing_store.write_merge_train_batch_landing_plan_record(landing_record)
    return {
        "merge_train_batch_landing_plan_record_id": landing_record.record_id,
        "repository": landing_plan.repository,
        "base_branch": landing_plan.base_branch,
        "mode": "plan_landing",
        "controller_action": "plan_landing",
        "landing_plan": landing_plan.model_dump(mode="json"),
    }


def _advance_without_candidate_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    transport: MergeTrainGitHubTransport,
    github_client: GitHubMergeTrainClient,
    trace_id: str,
    recorded_at: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
) -> dict[str, object]:
    waiting_collapse_record = latest_merge_train_stack_collapse_plan_record(
        record_store=stack_collapse_store,
        repository=request.repository,
        base_branch=request.base_branch,
        plan_status="waiting_for_root_checks",
    )
    if waiting_collapse_record is not None:
        waiting_result = _advance_waiting_stack_collapse_record(
            request=request,
            policy=policy,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            transport=transport,
            candidate_store=candidate_store,
            stack_collapse_store=stack_collapse_store,
            waiting_collapse_record=waiting_collapse_record,
            trace_id=trace_id,
            recorded_at=recorded_at,
        )
        if waiting_result is not None:
            return waiting_result

    planned_collapse_record = latest_merge_train_stack_collapse_plan_record(
        record_store=stack_collapse_store,
        repository=request.repository,
        base_branch=request.base_branch,
        plan_status="planned",
    )
    if planned_collapse_record is not None:
        planned_result = _advance_planned_stack_collapse_record(
            request=request,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            transport=transport,
            github_client=github_client,
            stack_collapse_store=stack_collapse_store,
            planned_collapse_record=planned_collapse_record,
            trace_id=trace_id,
            recorded_at=recorded_at,
        )
        if planned_result is not None:
            return planned_result

    return _advance_from_live_snapshot(
        request=request,
        policy=policy,
        policy_sha256=policy_sha256,
        transport=transport,
        candidate_store=candidate_store,
        stack_collapse_store=stack_collapse_store,
        trace_id=trace_id,
        recorded_at=recorded_at,
    )


def _advance_waiting_stack_collapse_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    transport: MergeTrainGitHubTransport,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    waiting_collapse_record: MergeTrainStackCollapsePlanRecord,
    trace_id: str,
    recorded_at: str,
) -> dict[str, object] | None:
    snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
        repository=request.repository,
        base_branch=request.base_branch,
    )
    root_pull_request = next(
        (
            pull_request
            for pull_request in snapshot.pull_requests
            if pull_request.number == waiting_collapse_record.plan.root_pull_request_number
        ),
        None,
    )
    if (
        root_pull_request is None
        or root_pull_request.head_sha
        != stack_collapse_expected_root_head_sha(waiting_collapse_record.plan)
    ):
        return None
    try:
        validate_merge_train_stack_collapse_record_for_controller(
            collapse_record=waiting_collapse_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    root_snapshot = snapshot.model_copy(update={"pull_requests": (root_pull_request,)})
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=root_snapshot)
    if dry_run_result.intended_next_action != "merge":
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "wait_for_root_checks",
            "merge_train_stack_collapse_plan_record_id": waiting_collapse_record.record_id,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
        }
    if not request.mutate:
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "admit_collapsed_root",
            "merge_train_stack_collapse_plan_record_id": waiting_collapse_record.record_id,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
        }

    candidate = build_merge_train_batch_candidate(
        dry_run_result=dry_run_result,
        base_sha=root_snapshot.base_sha,
        policy_sha256=policy_sha256,
        created_at=recorded_at,
    )
    candidate_record = build_merge_train_batch_candidate_record(
        candidate=candidate,
        source=f"service:controller:stack-collapse-admit:{trace_id}",
        updated_at=recorded_at,
    )
    candidate_store.write_merge_train_batch_candidate_record(candidate_record)
    return {
        "merge_train_batch_candidate_record_id": candidate_record.record_id,
        "merge_train_stack_collapse_plan_record_id": waiting_collapse_record.record_id,
        "repository": candidate.repository,
        "base_branch": candidate.base_branch,
        "mode": "admit_collapsed_root",
        "controller_action": "admit_collapsed_root",
        "dry_run_result": dry_run_result.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
    }


def _advance_planned_stack_collapse_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    transport: MergeTrainGitHubTransport,
    github_client: GitHubMergeTrainClient,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    planned_collapse_record: MergeTrainStackCollapsePlanRecord,
    trace_id: str,
    recorded_at: str,
) -> dict[str, object] | None:
    snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
        repository=request.repository,
        base_branch=request.base_branch,
    )
    root_pull_request = next(
        (
            pull_request
            for pull_request in snapshot.pull_requests
            if pull_request.number == planned_collapse_record.plan.root_pull_request_number
        ),
        None,
    )
    if (
        root_pull_request is None
        or root_pull_request.head_sha != planned_collapse_record.plan.root_initial_head_sha
    ):
        return None
    try:
        validate_merge_train_stack_collapse_record_for_controller(
            collapse_record=planned_collapse_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    result: dict[str, object] = {
        "repository": request.repository,
        "base_branch": request.base_branch,
        "mode": "dry-run" if not request.mutate else "execute_stack_collapse",
        "controller_action": "execute_stack_collapse",
        "merge_train_stack_collapse_plan_record_id": planned_collapse_record.record_id,
    }
    if request.mutate:
        executed_plan = execute_merge_train_stack_collapse_plan(
            plan=planned_collapse_record.plan,
            branch_client=github_client,
            updated_at=recorded_at,
        )
        executed_record = build_merge_train_stack_collapse_plan_record(
            plan=executed_plan,
            source=f"service:controller:stack-collapse-execute:{trace_id}",
            updated_at=recorded_at,
        )
        stack_collapse_store.write_merge_train_stack_collapse_plan_record(executed_record)
        result["merge_train_stack_collapse_plan_record_id"] = executed_record.record_id
        result["stack_collapse_plan"] = executed_plan.model_dump(mode="json")
    else:
        result["stack_collapse_plan"] = planned_collapse_record.plan.model_dump(mode="json")
    return result


def _advance_from_live_snapshot(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    transport: MergeTrainGitHubTransport,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    trace_id: str,
    recorded_at: str,
) -> dict[str, object]:
    snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
        repository=request.repository,
        base_branch=request.base_branch,
    )
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    selected_pr = dry_run_result.selected_pr
    if selected_pr is not None and merge_train_snapshot_has_stack_topology(
        snapshot=snapshot, dry_run_result=dry_run_result
    ):
        stack_discovery = discover_merge_train_stack(
            snapshot=snapshot,
            root_pull_request_number=selected_pr.number,
        )
    else:
        stack_discovery = None
    if stack_discovery is not None and stack_discovery.status == "ready_for_collapse":
        controller_action = "plan_stack_collapse"
        stack_collapse_plan = build_merge_train_stack_collapse_plan(
            discovery_result=stack_discovery,
            policy_key=dry_run_result.policy_key,
            policy_sha256=policy_sha256,
            created_at=recorded_at,
        )
        result: dict[str, object] = {
            "repository": stack_collapse_plan.repository,
            "base_branch": stack_collapse_plan.base_branch,
            "mode": "dry-run" if not request.mutate else controller_action,
            "controller_action": controller_action,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
            "stack_discovery": stack_discovery.model_dump(mode="json"),
            "stack_collapse_plan": stack_collapse_plan.model_dump(mode="json"),
        }
        if request.mutate:
            stack_collapse_record = build_merge_train_stack_collapse_plan_record(
                plan=stack_collapse_plan,
                source=f"service:controller:stack-collapse-plan:{trace_id}",
                updated_at=recorded_at,
            )
            stack_collapse_store.write_merge_train_stack_collapse_plan_record(stack_collapse_record)
            result["merge_train_stack_collapse_plan_record_id"] = stack_collapse_record.record_id
        return result
    if stack_discovery is not None and stack_discovery.status == "unsupported":
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "stack_unsupported",
            "dry_run_result": dry_run_result.model_dump(mode="json"),
            "stack_discovery": stack_discovery.model_dump(mode="json"),
        }
    if dry_run_result.intended_next_action == "idle":
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "idle",
            "dry_run_result": dry_run_result.model_dump(mode="json"),
        }
    if dry_run_result.intended_next_action != "merge":
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": dry_run_result.intended_next_action,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
        }

    controller_action = "plan_candidate"
    candidate = build_merge_train_batch_candidate(
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
        policy_sha256=policy_sha256,
        created_at=recorded_at,
    )
    result = {
        "repository": candidate.repository,
        "base_branch": candidate.base_branch,
        "mode": "dry-run" if not request.mutate else controller_action,
        "controller_action": controller_action,
        "dry_run_result": dry_run_result.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
    }
    if request.mutate:
        candidate_record = build_merge_train_batch_candidate_record(
            candidate=candidate,
            source=f"service:controller:candidate-plan:{trace_id}",
            updated_at=recorded_at,
        )
        candidate_store.write_merge_train_batch_candidate_record(candidate_record)
        result["merge_train_batch_candidate_record_id"] = candidate_record.record_id
    return result


def stale_merge_train_landing_plan(
    landing_plan: MergeTrainBatchLandingPlan,
) -> MergeTrainBatchLandingPlan:
    return landing_plan.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(update={"status": "stale"})
                if entry.status in {"planned", "merging"}
                else entry
                for entry in landing_plan.entries
            )
        }
    )


def try_reflow_failed_merge_train_candidate(
    *,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    active_candidate_record: MergeTrainBatchCandidateRecord,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    transport: MergeTrainGitHubTransport,
    repository: str,
    base_branch: str,
    recorded_at: str,
    trace_id: str,
    mutate: bool,
) -> dict[str, object] | None:
    try:
        snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
            repository=repository,
            base_branch=base_branch,
        )
    except Exception:
        return None
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    if dry_run_result.intended_next_action != "merge":
        return None
    if _merge_train_candidate_matches_dry_run_queue(
        candidate=active_candidate_record.candidate,
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
    ):
        return None
    candidate = build_merge_train_batch_candidate(
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
        policy_sha256=policy_sha256,
        created_at=recorded_at,
    )
    result: dict[str, object] = {
        "repository": candidate.repository,
        "base_branch": candidate.base_branch,
        "mode": "dry-run" if not mutate else "plan_candidate",
        "controller_action": "plan_candidate",
        "superseded_merge_train_batch_candidate_record_id": active_candidate_record.record_id,
        "dry_run_result": dry_run_result.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
    }
    if mutate:
        candidate_record = build_merge_train_batch_candidate_record(
            candidate=candidate,
            source=f"service:controller:candidate-reflow:{trace_id}",
            updated_at=recorded_at,
        )
        candidate_store.write_merge_train_batch_candidate_record(candidate_record)
        try:
            _supersede_active_merge_train_batch_candidate_records(
                record_store=candidate_store,
                repository=repository,
                base_branch=base_branch,
                batch_id=active_candidate_record.candidate.batch_id,
                replacement_record_id=candidate_record.record_id,
            )
        except Exception:
            candidate_store.write_merge_train_batch_candidate_record(
                candidate_record.model_copy(update={"status": "superseded"})
            )
            raise
        result["merge_train_batch_candidate_record_id"] = candidate_record.record_id
    return result


def validate_merge_train_candidate_record_for_controller(
    *,
    candidate_record: MergeTrainBatchCandidateRecord,
    policy_key: str,
    policy_sha256: str,
) -> None:
    if candidate_record.candidate.policy_key != policy_key:
        raise ValueError("merge train candidate policy key no longer matches")
    if candidate_record.candidate.policy_sha256 != policy_sha256:
        raise ValueError("merge train candidate policy digest no longer matches")


def validate_merge_train_landing_record_for_controller(
    *,
    landing_record: MergeTrainBatchLandingPlanRecord,
    policy_key: str,
    policy_sha256: str,
) -> None:
    if landing_record.landing_plan.policy_key != policy_key:
        raise ValueError("merge train landing plan policy key no longer matches")
    if landing_record.landing_plan.policy_sha256 != policy_sha256:
        raise ValueError("merge train landing plan policy digest no longer matches")


def validate_merge_train_stack_collapse_record_for_controller(
    *,
    collapse_record: MergeTrainStackCollapsePlanRecord,
    policy_key: str,
    policy_sha256: str,
) -> None:
    if collapse_record.plan.policy_key != policy_key:
        raise ValueError("merge train stack collapse policy key no longer matches")
    if collapse_record.plan.policy_sha256 != policy_sha256:
        raise ValueError("merge train stack collapse policy digest no longer matches")


def cleanup_merge_train_batch_candidate_ref(
    *,
    github_client: GitHubMergeTrainClient,
    landing_plan: MergeTrainBatchLandingPlan,
    trace_id: str,
) -> dict[str, object]:
    try:
        deleted = github_client.cleanup_batch_candidate_ref(landing_plan=landing_plan)
    except MergeTrainGitHubError as error:
        message = str(error).strip() or "GitHub candidate ref cleanup failed."
        _LOGGER.warning(
            "Merge train candidate ref cleanup failed after landing persistence",
            extra={
                "trace_id": trace_id,
                "repository": landing_plan.repository,
                "base_branch": landing_plan.base_branch,
                "candidate_ref": landing_plan.candidate_ref,
                "github_status_code": error.status_code,
            },
        )
        result: dict[str, object] = {
            "candidate_ref_cleanup_status": "failed",
            "candidate_ref_cleanup_message": message,
        }
        if error.status_code is not None:
            result["candidate_ref_cleanup_github_status_code"] = error.status_code
        return result
    return {
        "candidate_ref_cleanup_status": "deleted" if deleted else "already_missing",
    }


def _merge_train_stack_collapse_record_matches_landing_plan(
    *,
    collapse_record: MergeTrainStackCollapsePlanRecord,
    landing_plan: MergeTrainBatchLandingPlan,
    policy_sha256: str,
) -> bool:
    try:
        validate_stack_collapse_record_for_landing(
            collapse_record=collapse_record,
            landing_plan=landing_plan,
            policy_sha256=policy_sha256,
        )
    except ValueError:
        return False
    return True


def _merge_train_candidate_matches_dry_run_queue(
    *, candidate: MergeTrainBatchCandidate, dry_run_result: MergeTrainDryRunResult, base_sha: str
) -> bool:
    if candidate.repository != dry_run_result.repository:
        return False
    if candidate.base_branch != dry_run_result.base_branch:
        return False
    if candidate.base_sha != base_sha:
        return False
    candidate_entries = tuple(
        (entry.pull_request_number, entry.head_sha) for entry in candidate.entries
    )
    queue_by_number = {entry.number: entry for entry in dry_run_result.queue}
    current_entries: list[tuple[int, str]] = []
    for pull_request_number in dry_run_result.queue_order:
        queue_entry = queue_by_number[pull_request_number]
        if not queue_entry.eligible:
            return False
        current_entries.append((queue_entry.number, queue_entry.head_sha))
    return candidate_entries == tuple(current_entries)


def _supersede_active_merge_train_batch_candidate_records(
    *,
    record_store: MergeTrainBatchCandidateRecordStore,
    repository: str,
    base_branch: str,
    batch_id: str,
    replacement_record_id: str,
) -> None:
    records = record_store.list_merge_train_batch_candidate_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
    )
    for record in records:
        if record.record_id == replacement_record_id:
            continue
        if record.candidate.batch_id != batch_id:
            continue
        record_store.write_merge_train_batch_candidate_record(
            record.model_copy(update={"status": "superseded"})
        )


def _records_for_result(result: dict[str, object]) -> dict[str, str]:
    record_keys = (
        "merge_train_batch_candidate_record_id",
        "merge_train_batch_landing_plan_record_id",
        "merge_train_stack_collapse_plan_record_id",
        "superseded_merge_train_batch_candidate_record_id",
    )
    return {
        key: value for key in record_keys if isinstance((value := result.get(key)), str) and value
    }
