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

from control_plane.github_app_webhook import GitHubAppWebhookStore, _mapping, _positive_id, _string
from control_plane.merge_train_policy_source import (
    MergeTrainPolicyStoreMissingError,
    resolve_merge_train_policy_record,
)
from control_plane.repository_inventory import get_repository_inventory_read_model

MERGE_TRAIN_EVENT_CHANNEL = "launchplane_merge_train_wake"
_LOGGER = logging.getLogger(__name__)


def wake_merge_train_for_event(
    record_store: object, event: str, payload: dict[str, object]
) -> bool:
    """Select only inventoried, enabled trains; never use event data as merge evidence."""
    action = _string(payload, "action").lower()
    relevant = (
        (event == "pull_request" and action in {"labeled", "opened", "reopened", "synchronize"})
        or (event in {"check_run", "check_suite", "workflow_run"} and action == "completed")
        or (event == "status" and _string(payload, "state") in {"success", "failure", "error"})
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
            if target.base_branch == base and (action != "labeled" or target.enqueue_label == label)
        )
    if not policies:
        return False
    notify = getattr(record_store, "notify_merge_train", None)
    if not callable(notify):
        return False
    notify()
    return True


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
    ) -> None:
        deadline = monotonic() + timeout
        try:
            if self.connection is None:
                self.connection = _connect(self.database_url)
                self.connection.execute(f"LISTEN {MERGE_TRAIN_EVENT_CHANNEL}")
            while not stop_event.is_set():
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return
                received = False
                # Fully consume the received batch: closing a generator after
                # its first yield can retain backlog or discard a partial batch.
                for _ in self.connection.notifies(timeout=min(remaining, 1), stop_after=1):
                    received = True
                if received:
                    # Fold hints queued while the controller was busy into one
                    # fresh pass; timeout=0 polls without waiting for new events.
                    for _ in self.connection.notifies(timeout=0):
                        pass
                    return
        except Exception:  # noqa: BLE001 - the sweep remains available when notifications fail.
            _LOGGER.warning("Merge train event listener unavailable; using timed sweep.")
            self.close()
            stop_event.wait(timeout=max(0, deadline - monotonic()))
