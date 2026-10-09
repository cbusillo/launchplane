"""Run every enabled merge-train target on Launchplane's own clock.

GitHub runs scheduled workflows on a best-effort basis and dropped most of the
Merge Train Runner's five-minute runs, so the train advanced about every six
hours. This worker runs the same pass a scheduled workflow run made: read the
target's admission and, when admitted, run its runner mode once. The policy
record's ``scheduler`` block stays the switch: ``enabled`` selects the target and
``mutate`` decides whether the pass may act.
"""

import logging
import time
from contextlib import nullcontext
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import cast
from uuid import uuid4

from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerLeaseHeldError,
)
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    MergeTrainRepositoryPolicy,
)
from control_plane.github_request_timing import github_request_tally
from control_plane.merge_admission import require_merge_admission_record_store
from control_plane.merge_admission_live import LiveMergeAdmissionEvaluator
from control_plane.merge_admission import MergeAdmissionDeniedError
from control_plane.merge_train_branch_refresh import (
    optional_merge_train_branch_refresh_store,
    require_merge_train_client_review_read_store,
)
from control_plane.merge_train_admission import (
    MergeTrainRunHistoryStore,
    evaluate_merge_train_admission_from_store,
)
from control_plane.merge_train_batch_candidate import (
    require_merge_train_batch_candidate_record_store,
)
from control_plane.merge_train_batch_landing import (
    require_merge_train_batch_landing_plan_record_store,
)
from control_plane.merge_train_controller_feedback import build_feedback_payloads
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    execute_merge_train_controller_run_once,
    require_merge_train_controller_state_record_store,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.merge_train_github_token import resolve_merge_train_github_token
from control_plane.merge_train_policy_source import (
    MergeTrainPolicyStoreMissingError,
    resolve_merge_train_policy_record,
)
from control_plane.merge_train_pr_feedback import (
    MergeTrainPrFeedbackEnvelope,
    build_merge_train_pr_feedback_record,
    require_merge_train_pr_feedback_record_store,
)
from control_plane.merge_train_run_once import (
    MergeTrainRunOnceEnvelope,
    execute_recorded_merge_train_run_once,
    require_merge_train_run_record_store,
)
from control_plane.merge_train_stack_collapse import (
    require_merge_train_stack_collapse_plan_record_store,
)
from control_plane.repository_evidence import GitHubRepositoryEvidenceProvider
from control_plane.workflows.launchplane import github_api_request
from control_plane.workflows.ship import utc_now_timestamp

DEFAULT_MERGE_TRAIN_SCHEDULER_INTERVAL_SECONDS = 300
MERGE_TRAIN_SCHEDULER_FEEDBACK_SOURCE = "launchplane:merge-train-scheduler"
_LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeTrainScheduledTargetResult:
    repository: str
    base_branch: str
    runner_mode: str
    mutate: bool
    # deferred: admission said not yet; ran: the runner mode ran once;
    # failed: the pass could not run, and reason_code says why.
    status: str
    reason_code: str = ""
    trace_id: str = ""
    records: dict[str, str] = field(default_factory=dict)
    feedback_delivered: int = 0
    feedback_failed: int = 0
    continue_pass: bool = False


def run_merge_train_scheduler_pass(
    *,
    record_store: object,
    control_plane_root: Path,
    now: Callable[[], str] = utc_now_timestamp,
    should_stop: Callable[[], bool] = lambda: False,
) -> tuple[MergeTrainScheduledTargetResult, ...]:
    try:
        policy_record = resolve_merge_train_policy_record(record_store)
    except MergeTrainPolicyStoreMissingError:
        return ()
    scheduled_policies = sorted(
        (policy for policy in policy_record.policy.policies if policy.scheduler.enabled),
        key=lambda policy: (policy.repository, policy.base_branch),
    )
    results: list[MergeTrainScheduledTargetResult] = []
    for listed_policy in scheduled_policies:
        # Shutting down: finish the current target, never start another.
        if should_stop():
            break
        repository_policy = listed_policy
        # Each train is independent: one failing target never stops the others.
        try:
            # An earlier target can take minutes; act only on the policy as it is now.
            policy_record = resolve_merge_train_policy_record(record_store)
            current_policy = _current_scheduled_policy(policy_record, listed_policy)
            if current_policy is None:
                continue
            repository_policy = current_policy
            # Reacquire the normal lease and reread policy/admission at every
            # action. Bound work so one busy train cannot starve other targets.
            for _ in range(16):
                result = _run_scheduled_target(
                    record_store=record_store,
                    control_plane_root=control_plane_root,
                    policy_record=policy_record,
                    repository_policy=repository_policy,
                    now=now,
                )
                results.append(result)
                if not result.continue_pass or should_stop():
                    break
                policy_record = resolve_merge_train_policy_record(record_store)
                current_policy = _current_scheduled_policy(policy_record, listed_policy)
                if current_policy is None:
                    break
                repository_policy = current_policy
            continue
        except MergeTrainControllerLeaseHeldError:
            # A manual run or another worker holds this train; the next pass retries.
            result = _target_result(
                repository_policy, status="deferred", reason_code="controller_lease_held"
            )
        except Exception as error:  # noqa: BLE001 - recorded per target, then the pass moves on.
            _LOGGER.exception(
                "Scheduled merge train pass failed for %s@%s.",
                repository_policy.repository,
                repository_policy.base_branch,
            )
            result = _target_result(
                repository_policy, status="failed", reason_code=type(error).__name__
            )
        results.append(result)
    return tuple(results)


def run_merge_train_scheduler_loop(
    *,
    record_store: object,
    control_plane_root: Path,
    interval_seconds: int = DEFAULT_MERGE_TRAIN_SCHEDULER_INTERVAL_SECONDS,
    stop_event: Event | None = None,
    max_passes: int | None = None,
    pass_callback: Callable[[tuple[MergeTrainScheduledTargetResult, ...]], None] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    wait_for_event: Callable[[float, Event], None] | None = None,
) -> int:
    if interval_seconds < 1:
        raise ValueError("Merge train scheduler interval_seconds must be positive.")
    if max_passes is not None and max_passes < 1:
        raise ValueError("Merge train scheduler max_passes must be positive.")
    scheduler_stop_event = stop_event or Event()
    passes = 0
    while not scheduler_stop_event.is_set():
        started_at = monotonic()
        try:
            results = run_merge_train_scheduler_pass(
                record_store=record_store,
                control_plane_root=control_plane_root,
                should_stop=scheduler_stop_event.is_set,
            )
        except Exception:  # noqa: BLE001 - a bad pass (say, an unreadable policy) waits one interval.
            _LOGGER.exception("Scheduled merge train pass failed.")
            results = ()
        passes += 1
        if pass_callback is not None:
            pass_callback(results)
        if max_passes is not None and passes >= max_passes:
            break
        # Start passes on the interval; a long landing only shortens the wait.
        elapsed = monotonic() - started_at
        timeout = max(0.0, interval_seconds - elapsed)
        if wait_for_event is None:
            scheduler_stop_event.wait(timeout=timeout)
        else:
            wait_for_event(timeout, scheduler_stop_event)
    return passes


def _current_scheduled_policy(
    policy_record: MergeTrainPolicyRecord, listed_policy: MergeTrainRepositoryPolicy
) -> MergeTrainRepositoryPolicy | None:
    try:
        current_policy = policy_record.policy.find_repository_policy(
            repository=listed_policy.repository, base_branch=listed_policy.base_branch
        )
    except ValueError:
        return None
    return current_policy if current_policy.scheduler.enabled else None


def _run_scheduled_target(
    *,
    record_store: object,
    control_plane_root: Path,
    policy_record: MergeTrainPolicyRecord,
    repository_policy: MergeTrainRepositoryPolicy,
    now: Callable[[], str],
) -> MergeTrainScheduledTargetResult:
    admission = evaluate_merge_train_admission_from_store(
        store=cast(MergeTrainRunHistoryStore, record_store),
        repository=repository_policy.repository,
        base_branch=repository_policy.base_branch,
        requested_at=now(),
        current_policy_key=repository_policy.policy_key,
        current_policy_sha256=policy_record.policy_sha256,
        policy_record=policy_record,
    )
    if admission.status != "admitted":
        return _target_result(
            repository_policy, status="deferred", reason_code=str(admission.reason_code)
        )
    token = resolve_merge_train_github_token(
        source=repository_policy.github_token,
        repository=repository_policy.repository,
        control_plane_root=control_plane_root,
    )
    if not token:
        return _target_result(
            repository_policy, status="failed", reason_code="github_token_not_configured"
        )
    trace_id = f"launchplane_scheduler_{uuid4().hex}"
    if repository_policy.scheduler.runner_mode == "level1":
        return _run_level1(
            record_store=record_store,
            policy_record=policy_record,
            repository_policy=repository_policy,
            token=token,
            trace_id=trace_id,
            now=now,
        )
    return _run_controller(
        record_store=record_store,
        control_plane_root=control_plane_root,
        policy_record=policy_record,
        repository_policy=repository_policy,
        token=token,
        trace_id=trace_id,
        now=now,
    )


def _run_level1(
    *,
    record_store: object,
    policy_record: MergeTrainPolicyRecord,
    repository_policy: MergeTrainRepositoryPolicy,
    token: str,
    trace_id: str,
    now: Callable[[], str],
) -> MergeTrainScheduledTargetResult:
    mutate = repository_policy.scheduler.mutate
    try:
        review_store = require_merge_train_client_review_read_store(
            record_store, route="Scheduled merge train run-once"
        )
    except MergeAdmissionDeniedError as error:
        return _target_result(repository_policy, status="failed", reason_code=error.reason_code)
    result = execute_recorded_merge_train_run_once(
        request=MergeTrainRunOnceEnvelope(
            repository=repository_policy.repository,
            base_branch=repository_policy.base_branch,
            mutate=mutate,
        ),
        policy=policy_record.policy,
        policy_sha256=policy_record.policy_sha256,
        repository_policy=repository_policy,
        token=token,
        trace_id=trace_id,
        recorded_at=now(),
        run_record_store=require_merge_train_run_record_store(record_store),
        review_store=review_store,
        controller_state_store=(
            require_merge_train_controller_state_record_store(record_store) if mutate else None
        ),
    )
    return _target_result(
        repository_policy, status="ran", trace_id=trace_id, records=result.records
    )


def _run_controller(
    *,
    record_store: object,
    control_plane_root: Path,
    policy_record: MergeTrainPolicyRecord,
    repository_policy: MergeTrainRepositoryPolicy,
    token: str,
    trace_id: str,
    now: Callable[[], str],
) -> MergeTrainScheduledTargetResult:
    request = MergeTrainControllerRunOnceEnvelope(
        repository=repository_policy.repository,
        base_branch=repository_policy.base_branch,
        mutate=repository_policy.scheduler.mutate,
    )
    # The same wiring as the controller route: admission reads the pull request
    # with the policy's own credential.
    admission_evaluator = LiveMergeAdmissionEvaluator(
        store=record_store,
        repository_evidence_provider=GitHubRepositoryEvidenceProvider(
            control_plane_root=control_plane_root,
            github_token=lambda **_: token,
            github_token_scope=lambda **_: nullcontext(token),
            github_api=github_api_request,
            token_context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ),
        technical_check_client=GitHubMergeTrainClient(
            transport=UrllibMergeTrainGitHubTransport(
                token=token,
                api_base_url=request.github_api_base_url,
            )
        ),
    )
    with github_request_tally(
        f"scheduled merge-train controller {request.repository}"
        f"@{request.base_branch} trace {trace_id}"
    ):
        controller_result = execute_merge_train_controller_run_once(
            request=request,
            policy=policy_record.policy,
            policy_sha256=policy_record.policy_sha256,
            repository_policy=repository_policy,
            token=token,
            trace_id=trace_id,
            recorded_at=now(),
            candidate_store=require_merge_train_batch_candidate_record_store(record_store),
            landing_store=require_merge_train_batch_landing_plan_record_store(record_store),
            stack_collapse_store=require_merge_train_stack_collapse_plan_record_store(record_store),
            controller_state_store=require_merge_train_controller_state_record_store(record_store),
            admission_store=require_merge_admission_record_store(record_store),
            admission_evaluator=admission_evaluator,
            branch_refresh_store=optional_merge_train_branch_refresh_store(record_store),
        )
    delivered = 0
    failed = 0
    # Dry-run passes never comment on pull requests.
    if request.mutate:
        delivered, failed = _deliver_controller_feedback(
            record_store=record_store,
            policy_record=policy_record,
            repository_policy=repository_policy,
            token=token,
            trace_id=trace_id,
            response={
                "result": controller_result.accepted_result,
                "records": controller_result.records,
            },
            now=now,
        )
    return _target_result(
        repository_policy,
        status="ran",
        trace_id=trace_id,
        records=controller_result.records,
        feedback_delivered=delivered,
        feedback_failed=failed,
        continue_pass=request.mutate
        and _controller_can_continue(controller_result.accepted_result),
    )


def _controller_can_continue(result: dict[str, object]) -> bool:
    if result.get("error"):
        return False
    action = result.get("controller_action")
    if action in {
        "plan_candidate",
        "admit_collapsed_root",
        "plan_landing",
        "plan_stack_collapse",
        "retire_stale_landing",
    }:
        return True
    candidate = result.get("candidate")
    if action == "build_candidate" and isinstance(candidate, dict):
        return candidate.get("status") in {"ready_for_checks", "passed"}
    if action == "observe_candidate" and isinstance(candidate, dict):
        return candidate.get("status") == "passed"
    # Branch updates, stack collapse and candidate publication start CI. Blocks,
    # waits, reconciliation and landing outcomes end this bounded pass.
    return False


def _deliver_controller_feedback(
    *,
    record_store: object,
    policy_record: MergeTrainPolicyRecord,
    repository_policy: MergeTrainRepositoryPolicy,
    token: str,
    trace_id: str,
    response: dict[str, object],
    now: Callable[[], str],
) -> tuple[int, int]:
    payloads = build_feedback_payloads(
        response=response, source=MERGE_TRAIN_SCHEDULER_FEEDBACK_SOURCE
    )
    if not payloads:
        return 0, 0
    feedback_store = require_merge_train_pr_feedback_record_store(record_store)
    delivered = 0
    failed = 0
    for payload in payloads:
        feedback_record = build_merge_train_pr_feedback_record(
            request=MergeTrainPrFeedbackEnvelope.model_validate(payload),
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_record.policy_sha256,
            token=token,
            recorded_at=now(),
            response_trace_id=trace_id,
        )
        feedback_store.write_merge_train_pr_feedback_record(feedback_record)
        if feedback_record.delivery_status == "failed":
            failed += 1
        else:
            delivered += 1
    return delivered, failed


def _target_result(
    repository_policy: MergeTrainRepositoryPolicy,
    *,
    status: str,
    reason_code: str = "",
    trace_id: str = "",
    records: dict[str, str] | None = None,
    feedback_delivered: int = 0,
    feedback_failed: int = 0,
    continue_pass: bool = False,
) -> MergeTrainScheduledTargetResult:
    return MergeTrainScheduledTargetResult(
        repository=repository_policy.repository,
        base_branch=repository_policy.base_branch,
        runner_mode=repository_policy.scheduler.runner_mode,
        mutate=repository_policy.scheduler.mutate,
        status=status,
        reason_code=reason_code,
        trace_id=trace_id,
        records=dict(records or {}),
        feedback_delivered=feedback_delivered,
        feedback_failed=feedback_failed,
        continue_pass=continue_pass,
    )
