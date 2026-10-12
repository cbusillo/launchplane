"""Observe trains independently; alerts never hold, reset or merge a train."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
from threading import Event
from typing import Protocol, cast
from urllib.parse import quote

from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane import runtime_environments
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.contracts.merge_train_stall import (
    MergeTrainStallObservation,
    MergeTrainStallRecord,
    MergeTrainStallWorkflow,
)
from control_plane.launchplane_github_delivery import resolve_delivery_github_token
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train_github import GitHubMergeTrainClient, UrllibMergeTrainGitHubTransport
from control_plane.merge_train_github_token import resolve_merge_train_github_token
from control_plane.merge_train_policy_source import resolve_merge_train_policy_record
from control_plane.workflows.ship import utc_now_timestamp

STALL_SECONDS = 30 * 60
STALL_WORKFLOW_REPOSITORY_KEY = "LAUNCHPLANE_MERGE_TRAIN_STALL_ALERT_REPOSITORY"
STALL_WORKFLOW_REF_KEY = "LAUNCHPLANE_MERGE_TRAIN_STALL_ALERT_REF"
STALL_ISSUE_MARKER = "<!-- launchplane:merge-train-stall -->"
_LOGGER = logging.getLogger(__name__)


class StallStore(Protocol):
    def merge_train_stall_lock(self, *, repository: str) -> AbstractContextManager[None]: ...
    def read_merge_train_stall_record(self, repository: str) -> MergeTrainStallRecord: ...
    def write_merge_train_stall_record(self, record: MergeTrainStallRecord) -> object: ...
    def list_merge_train_controller_state_records(
        self, **kwargs: object
    ) -> tuple[MergeTrainControllerStateRecord, ...]: ...


def _age(now: str, since: str) -> float:
    return (
        datetime.fromisoformat(now.replace("Z", "+00:00"))
        - datetime.fromisoformat(since.replace("Z", "+00:00"))
    ).total_seconds()


def observe_stall(
    *,
    previous: MergeTrainStallObservation,
    now: str,
    progress_key: str,
    ready: tuple[str, ...],
    busy: bool,
    orphaned_lease: bool = False,
    recovered_failure: bool = False,
) -> MergeTrainStallObservation:
    changed = progress_key != previous.progress_key
    since = now if changed or not previous.progress_since else previous.progress_since
    ready_since = {key: previous.ready_since.get(key, now) for key in ready}
    reasons: list[str] = []
    if busy and _age(now, since) >= STALL_SECONDS:
        reasons.append("controller_unchanged")
    if any(_age(now, started) >= STALL_SECONDS for started in ready_since.values()):
        reasons.append("ready_change_not_moving")
    signals: list[str] = []
    if orphaned_lease:
        signals.append("lease_without_live_heartbeat")
    if recovered_failure:
        signals.append("candidate_failed_after_passing_rerun")
    # The controller may be between release/recovery passes. Confirm transient
    # signals in the next independent observation before waking people.
    if not changed:
        reasons.extend(signal for signal in signals if signal in previous.suspected_signals)
    return MergeTrainStallObservation(
        progress_key=progress_key,
        progress_since=since,
        ready_since=ready_since,
        reasons=tuple(reasons),
        suspected_signals=tuple(signals),
    )


def resolve_stall_workflow(root: Path) -> MergeTrainStallWorkflow | None:
    definition = runtime_environments.load_runtime_environment_definition(control_plane_root=root)
    context = definition.contexts.get("launchplane")
    values = {**definition.shared_env, **(context.shared_env if context else {})}
    repository = str(values.get(STALL_WORKFLOW_REPOSITORY_KEY, "")).strip()
    ref = str(values.get(STALL_WORKFLOW_REF_KEY, "")).strip()
    if not repository and not ref:
        return None
    return MergeTrainStallWorkflow(repository=repository, ref=ref)


class GitHubStallReporter:
    """Reuse the delivery App and the existing Discord workflow secret."""

    def __init__(self, *, root: Path, repository: str) -> None:
        self.root = root
        self.repository = repository
        token = resolve_delivery_github_token(
            control_plane_root=root,
            context_name="launchplane",
            repository=repository,
            purpose="release_record",
        )
        self.transport = UrllibMergeTrainGitHubTransport(token=token)
        self.path = f"/repos/{repository}"

    def publish(self, record: MergeTrainStallRecord, body: str, *, stalled: bool) -> int | None:
        number = record.issue_number
        if number is None:
            # Reconcile a create whose reply was lost before creating again. Never
            # edit human requests or silently pick one of several matching issues.
            query = quote(
                f'repo:{self.repository} is:issue in:title "Merge train stalled"', safe=""
            )
            result = cast(
                dict[str, object],
                self.transport.request(method="GET", path=f"/search/issues?q={query}&per_page=100"),
            )
            items = cast(list[dict[str, object]], result["items"])
            if result.get("incomplete_results") or int(cast(int, result["total_count"])) > len(
                items
            ):
                raise ValueError("Incomplete stall issue reconciliation.")
            matches = [
                item
                for item in items
                if STALL_ISSUE_MARKER in str(item.get("body", ""))
                and cast(dict[str, object], item["user"]).get("type") == "Bot"
            ]
            if len(matches) > 1:
                raise ValueError("Ambiguous stall issue reconciliation.")
            if matches:
                number = int(cast(int, matches[0]["number"]))
            elif not stalled:
                return None
            else:
                created = cast(
                    dict[str, object],
                    self.transport.request(
                        method="POST",
                        path=f"{self.path}/issues",
                        body={"title": "Merge train stalled", "body": body},
                    ),
                )
                return int(cast(int, created["number"]))
        existing = cast(
            dict[str, object],
            self.transport.request(method="GET", path=f"{self.path}/issues/{number}"),
        )
        if (
            STALL_ISSUE_MARKER not in str(existing.get("body", ""))
            or cast(dict[str, object], existing["user"]).get("type") != "Bot"
        ):
            raise ValueError("Stall issue ownership changed.")
        state = "open" if stalled else "closed"
        if existing.get("body") != body or existing.get("state") != state:
            self.transport.request(
                method="PATCH",
                path=f"{self.path}/issues/{number}",
                body={"body": body, "state": state},
            )
        return number

    def notify(self, record: MergeTrainStallRecord, workflow: MergeTrainStallWorkflow) -> None:
        token = resolve_delivery_github_token(
            control_plane_root=self.root,
            context_name="launchplane",
            repository=workflow.repository,
            purpose="workflow_dispatch",
        )
        UrllibMergeTrainGitHubTransport(token=token).request(
            method="POST",
            path=f"/repos/{workflow.repository}/actions/workflows/merge-train-stall-alert.yml/dispatches",
            body={
                "ref": workflow.ref,
                "inputs": {
                    "repository": record.repository,
                    "issue_number": str(record.issue_number),
                    "state": record.issue_state,
                    "notification_key": record.notification_key,
                },
            },
        )


def _issue_body(record: MergeTrainStallRecord) -> str:
    lines = [STALL_ISSUE_MARKER, "The merge train needs the Supervisor's attention.", ""]
    for base, observation in sorted(record.observations.items()):
        if observation.reasons:
            lines.append(
                f"- Base `{base}`: {', '.join(observation.reasons)}; unchanged since {observation.progress_since}."
            )
            lines.extend(
                f"  - Ready change `{key}` since {since}."
                for key, since in sorted(observation.ready_since.items())
            )
    if not any(item.reasons for item in record.observations.values()):
        lines.append("Resolved: the train progressed and no stall condition remains.")
    lines.extend(
        [
            "",
            "The monitor does not merge, release leases, rerun checks or change readiness labels.",
            "Supervisor: diagnose/recover through the supported controller path.",
        ]
    )
    return "\n".join(lines)


def run_stall_monitor_pass(
    *,
    store: object,
    root: Path,
    now: Callable[[], str] = utc_now_timestamp,
    reporter_factory: Callable[..., GitHubStallReporter] = GitHubStallReporter,
) -> None:
    policy_record = resolve_merge_train_policy_record(store)
    targets: dict[str, list[MergeTrainRepositoryPolicy]] = {}
    for target in policy_record.policy.policies:
        if (
            target.scheduler.enabled
            and target.scheduler.mutate
            and target.scheduler.runner_mode == "controller"
        ):
            targets.setdefault(target.repository.lower(), []).append(target)
    stall_store = cast(StallStore, store)
    for repository, policies in targets.items():
        try:
            with stall_store.merge_train_stall_lock(repository=repository):
                observed_at = now()
                try:
                    record = stall_store.read_merge_train_stall_record(repository)
                except FileNotFoundError:
                    record = MergeTrainStallRecord(repository=repository, observed_at=observed_at)
                observations: dict[str, MergeTrainStallObservation] = {}
                for target in policies:
                    token = resolve_merge_train_github_token(
                        source=target.github_token,
                        repository=target.repository,
                        control_plane_root=root,
                    )
                    client = GitHubMergeTrainClient(
                        transport=UrllibMergeTrainGitHubTransport(token=token)
                    )
                    snapshot = client.read_merge_train_snapshot(
                        repository=repository, base_branch=target.base_branch
                    )
                    queue = build_merge_train_dry_run_result(
                        policy=policy_record.policy, snapshot=snapshot, batch_landing=True
                    )
                    states = stall_store.list_merge_train_controller_state_records(
                        repository=repository, base_branch=target.base_branch, limit=1
                    )
                    state = states[0] if states else None
                    candidate_records = getattr(store, "list_merge_train_batch_candidate_records")(
                        repository=repository,
                        base_branch=target.base_branch,
                        status="active",
                        limit=25,
                    )
                    current = tuple(
                        r
                        for r in candidate_records
                        if r.candidate.policy_sha256 == policy_record.policy_sha256
                        and r.candidate.base_sha == snapshot.base_sha
                    )
                    signatures = [
                        (r.record_id, r.candidate.status, r.candidate.candidate_sha)
                        for r in current
                    ]
                    state_signature = (
                        (
                            state.active_action or state.last_action,
                            state.active_phase or state.last_phase,
                            state.active_record_id or state.last_record_id,
                        )
                        if state
                        else ()
                    )
                    progress_key = json.dumps(
                        [snapshot.base_sha, state_signature, signatures], sort_keys=True
                    )
                    previous = record.observations.get(
                        target.base_branch, MergeTrainStallObservation()
                    )
                    # Participating in a progressing candidate resets that member's
                    # wait; another change joining the queue does not reset older waits.
                    participating = (
                        {
                            f"{e.pull_request_number}:{e.head_sha}"
                            for r in current
                            for e in r.candidate.entries
                        }
                        if progress_key != previous.progress_key
                        else set()
                    )
                    ready = tuple(f"{e.number}:{e.head_sha}" for e in queue.queue if e.eligible)
                    previous = previous.model_copy(
                        update={
                            "ready_since": {
                                k: v
                                for k, v in previous.ready_since.items()
                                if k not in participating
                            }
                        }
                    )
                    recovered = any(
                        r.candidate.status == "failed"
                        and r.candidate.required_checks_status == "fail"
                        and r.candidate.candidate_sha
                        and r.candidate.head_check_reuse is None
                        and client.observe_batch_candidate_checks(
                            candidate=r.candidate
                        ).required_checks_status
                        == "pass"
                        for r in current
                    )
                    observations[target.base_branch] = observe_stall(
                        previous=previous,
                        now=observed_at,
                        progress_key=progress_key,
                        ready=ready,
                        busy=bool(ready or current or (state and state.lease_owner)),
                        orphaned_lease=bool(
                            state
                            and state.lease_owner
                            and _age(observed_at, state.lease_expires_at) >= 0
                        ),
                        recovered_failure=recovered,
                    )
                previous_reasons = {
                    base: item.reasons for base, item in record.observations.items() if item.reasons
                }
                current_reasons = {
                    base: item.reasons for base, item in observations.items() if item.reasons
                }
                record = record.model_copy(
                    update={"observations": observations, "observed_at": observed_at}
                )
                stalled = any(o.reasons for o in observations.values())
                body = _issue_body(record)
                body_digest = hashlib.sha256(body.encode()).hexdigest()
                # Preserve the observation clock even if issue delivery is unavailable.
                stall_store.write_merge_train_stall_record(record)
                if not stalled and record.issue_number is None:
                    continue
                try:
                    reporter = reporter_factory(root=root, repository=repository)
                    number = reporter.publish(record, body, stalled=stalled)
                except Exception as error:
                    updates: dict[str, object] = {"delivery_error": type(error).__name__}
                    if isinstance(error, MergeTrainGitHubError) and error.status_code == 404:
                        updates.update({"issue_number": None, "notification_key": ""})
                    stall_store.write_merge_train_stall_record(record.model_copy(update=updates))
                    continue
                next_state = "open" if stalled else "closed"
                if number is not None and (
                    record.issue_state != next_state
                    or (stalled and previous_reasons != current_reasons)
                    or (stalled and not record.notification_key)
                ):
                    record = record.model_copy(
                        update={
                            "notification_key": hashlib.sha256(
                                f"{repository}:{observed_at}:{next_state}:{body_digest}".encode()
                            ).hexdigest()
                        }
                    )
                record = record.model_copy(
                    update={
                        "issue_number": number,
                        "issue_state": next_state,
                        "issue_body_sha256": body_digest,
                        "delivery_error": "",
                    }
                )
                stall_store.write_merge_train_stall_record(record)
                if record.notification_key != record.notified_key:
                    try:
                        workflow = resolve_stall_workflow(root)
                        if workflow is None:
                            record = record.model_copy(
                                update={"delivery_error": "stall_alert_workflow_not_configured"}
                            )
                        else:
                            reporter.notify(record, workflow)
                            record = record.model_copy(
                                update={"notified_key": record.notification_key}
                            )
                    except Exception as error:
                        record = record.model_copy(update={"delivery_error": type(error).__name__})
                    stall_store.write_merge_train_stall_record(record)
        except Exception as error:
            # Missing evidence never resolves an incident or stops other targets.
            _LOGGER.error(
                "Stall monitor unavailable for %s (%s).", repository, type(error).__name__
            )


def run_stall_monitor_loop(*, store: object, root: Path, stop_event: Event) -> None:
    while not stop_event.is_set():
        try:
            run_stall_monitor_pass(store=store, root=root)
        except Exception as error:
            _LOGGER.error("Stall monitor pass unavailable (%s).", type(error).__name__)
        stop_event.wait(300)
