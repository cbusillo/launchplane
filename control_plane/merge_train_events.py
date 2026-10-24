"""Signed source-control events wake the train; policy and live reads decide its work.

Notifications carry no runtime authority and need no table or migration. A worker
that is disconnected misses the hint and recovers on its next timed sweep.
"""

from collections.abc import Callable
import logging
from threading import Event
import time
from typing import cast

import psycopg
from sqlalchemy.engine import make_url

from control_plane.contracts.merge_train_controller_state import build_merge_train_controller_key
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.github_app_webhook import GitHubAppWebhookStore, _mapping, _positive_id, _string
from control_plane.merge_train_policy_source import (
    MergeTrainPolicyStoreMissingError,
    resolve_merge_train_policy_record,
)
from control_plane.repository_inventory import get_repository_inventory_read_model
from control_plane.merge_train_wake import (
    MergeTrainEventReceipt,
    MergeTrainWake,
    decode_merge_train_wake,
)
from control_plane.workflows.ship import utc_now_timestamp

MERGE_TRAIN_EVENT_CHANNEL = "launchplane_merge_train_wake"
_LOGGER = logging.getLogger(__name__)


def wake_merge_train_for_event(
    record_store: object,
    event: str,
    payload: dict[str, object],
    *,
    receipt: MergeTrainEventReceipt | None = None,
) -> bool:
    """Select only inventoried, enabled trains; never use event data as merge evidence."""
    action = _string(payload, "action").lower()
    completion = (
        event in {"check_run", "check_suite", "workflow_run"} and action == "completed"
    ) or (event == "status" and _string(payload, "state") in {"success", "failure", "error"})
    relevant = completion or (
        event == "pull_request"
        and action in {"labeled", "unlabeled", "opened", "reopened", "synchronize", "closed"}
    )
    if not relevant:
        return False
    repository_id = _positive_id(_mapping(payload, "repository"), "id")
    if not repository_id:
        return False
    try:
        policy = resolve_merge_train_policy_record(record_store).policy
    except MergeTrainPolicyStoreMissingError:
        return False
    inventory = get_repository_inventory_read_model(
        repository_id=repository_id, store=cast(GitHubAppWebhookStore, record_store)
    ).current_record
    if inventory is None or inventory.inventory_state != "tracked":
        return False
    policies = tuple(
        target
        for target in policy.policies
        if target.scheduler.enabled and target.repository.lower() == inventory.repository.lower()
    )
    if event == "pull_request":
        base = _string(_mapping(_mapping(payload, "pull_request"), "base"), "ref")
        label = _string(_mapping(payload, "label"), "name")
        policies = tuple(
            target
            for target in policies
            if target.base_branch == base
            and (action not in {"labeled", "unlabeled"} or target.enqueue_label == label)
        )
    else:
        train_bases = frozenset(target.base_branch for target in policies)
        policies = tuple(
            target
            for target in policies
            if _completion_concerns_target(event, payload, target, train_bases)
        )
    if not policies:
        return False
    notify = getattr(record_store, "notify_merge_train", None)
    if not callable(notify):
        return False
    notify(
        tuple(
            build_merge_train_controller_key(
                repository=target.repository, base_branch=target.base_branch
            )
            for target in policies
        ),
        receipt=receipt,
    )
    return True


def _completion_concerns_target(
    event: str,
    payload: dict[str, object],
    target: MergeTrainRepositoryPolicy,
    train_bases: frozenset[str],
) -> bool:
    """Whether a completed head could be this train's PR, candidate or base.

    A train waits only on checks of PRs rooted at its base (stacks included), its
    own candidate refs and its base branch. Labels are not in check payloads, so
    every such PR still wakes its train. A PR into another branch may be a stack
    child, so it wakes every train except when that branch is another train's
    base. A head with no PR on another branch, or another train's candidate,
    wakes nothing; fork PRs, which the train never admits, look the same.
    """
    if event == "status":
        branches = tuple(
            _string(branch, "name")
            for branch in _objects(payload.get("branches"))
            if _string(branch, "name")
        )
        pull_requests: tuple[dict[str, object], ...] = ()
    else:
        run = _mapping(payload, event)
        suite = _mapping(run, "check_suite") if event == "check_run" else run
        branches = tuple(branch for branch in (_string(suite, "head_branch"),) if branch)
        pull_requests = _objects(run.get("pull_requests")) if run else ()
    for pull_request in pull_requests:
        base = _string(_mapping(pull_request, "base"), "ref")
        if base == target.base_branch or base not in train_bases:
            return True
    candidate_prefix = (
        f"launchplane/train/{target.repository.lower()}/{target.base_branch.replace('/', '-')}/"
    )
    for branch in branches:
        if branch == target.base_branch or branch.lower().startswith(candidate_prefix):
            return True
        if branch.startswith("launchplane/train/ordinary/"):
            return True
        # A commit status carries no PR, so a PR head branch may be this train's.
        if event == "status" and not branch.startswith("launchplane/train/"):
            return True
    return False


def _objects(value: object) -> tuple[dict[str, object], ...]:
    if not isinstance(value, list):
        return ()
    return tuple(cast(dict[str, object], item) for item in value if isinstance(item, dict))


def _connect(database_url: str) -> psycopg.Connection[tuple[object, ...]]:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("Merge train notifications require PostgreSQL.")
    return psycopg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False),
        autocommit=True,
        connect_timeout=5,
    )


class MergeTrainEventListener:
    """A worker-owned connection, separate from ORM transactions and controller leases."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.connection: psycopg.Connection[tuple[object, ...]] | None = None

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
            self.connection = None

    def wait(
        self, timeout: float, stop_event: Event, *, monotonic: Callable[[], float] = time.monotonic
    ) -> MergeTrainWake:
        """Return routing keys, the actual wait outcome and bounded observed receipts.

        A timeout, a listener failure or an empty hint asks for every
        target, so an older publisher or a missed event never narrows the sweep.
        Due sweeps drain routing hints while retaining their advisory observations.
        """
        deadline = monotonic() + timeout
        try:
            if self.connection is None:
                self.connection = _connect(self.database_url)
                self.connection.execute(f"LISTEN {MERGE_TRAIN_EVENT_CHANNEL}")
            if timeout <= 0:
                # The sweep is due and rereads every train after these hints
                # arrived, so drop them rather than repeat their trains.
                observed_at = utc_now_timestamp()
                payloads = [item.payload for item in self.connection.notifies(timeout=0)]
                return decode_merge_train_wake(
                    payloads, reason="timer_sweep", hint_observed_at=observed_at if payloads else ""
                )
            while not stop_event.is_set():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return MergeTrainWake(None, "timer_sweep")
                payloads = []
                # Fully consume the received batch: closing a generator after
                # its first yield can retain backlog or discard a partial batch.
                for notification in self.connection.notifies(
                    timeout=min(remaining, 1), stop_after=1
                ):
                    payloads.append(notification.payload)
                if payloads:
                    observed_at = utc_now_timestamp()
                    # Fold hints queued while the controller was busy into one
                    # fresh pass; timeout=0 polls without waiting for new events.
                    for notification in self.connection.notifies(timeout=0):
                        payloads.append(notification.payload)
                    return decode_merge_train_wake(
                        payloads, reason="event_hints", hint_observed_at=observed_at
                    )
            return MergeTrainWake(None, "stopped")
        except Exception:  # noqa: BLE001 - the sweep remains available when notifications fail.
            _LOGGER.warning("Merge train event listener unavailable; using timed sweep.")
            self.close()
            stop_event.wait(timeout=max(0, deadline - monotonic()))
            return MergeTrainWake(None, "listener_fallback")
