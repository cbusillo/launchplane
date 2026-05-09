from __future__ import annotations

from typing import Protocol

from control_plane.contracts.merge_train_admission import MergeTrainAdmissionDecision
from control_plane.contracts.merge_train_admission import evaluate_merge_train_admission
from control_plane.contracts.merge_train_run_record import MergeTrainRunRecord


class MergeTrainRunHistoryStore(Protocol):
    def latest_merge_train_run_record(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainRunRecord | None: ...


def evaluate_merge_train_admission_from_store(
    *,
    store: MergeTrainRunHistoryStore,
    repository: str,
    base_branch: str,
    requested_at: str,
    poll_interval_seconds: int = 60,
    backoff_seconds: int = 300,
) -> MergeTrainAdmissionDecision:
    return evaluate_merge_train_admission(
        repository=repository,
        base_branch=base_branch,
        requested_at=requested_at,
        latest_run=store.latest_merge_train_run_record(
            repository=repository,
            base_branch=base_branch,
        ),
        poll_interval_seconds=poll_interval_seconds,
        backoff_seconds=backoff_seconds,
    )
