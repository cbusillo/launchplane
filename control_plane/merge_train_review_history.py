"""Bounded, read-only historical evidence for restoring original PR review."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel

from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
    MergeTrainStackCollapseRecordStatus,
    MergeTrainStackCollapseRunStatus,
)
from control_plane.merge_train_original_review import StackHistoryStore
from control_plane.workflows.merge_train_controller import (
    latest_merge_train_stack_collapse_progress_record,
)


class OriginalReviewHistoryChange(BaseModel):
    parent_pull_request_number: int
    parent_head_ref: str
    parent_initial_head_sha: str
    child_pull_request_number: int
    child_head_sha: str
    merge_commit_sha: str
    status: MergeTrainStackCollapseRunStatus


class OriginalReviewHistoryEntry(BaseModel):
    record_id: str
    collapse_id: str
    status: MergeTrainStackCollapseRecordStatus
    updated_at: str
    root_pull_request_number: int
    root_head_ref: str
    root_initial_head_sha: str
    original_changes: tuple[OriginalReviewHistoryChange, ...]


class OriginalReviewHistoryResponse(BaseModel):
    status: Literal["ok"] = "ok"
    trace_id: str
    repository: str
    base_branch: str
    history_digest: str
    entries: tuple[OriginalReviewHistoryEntry, ...]
    next_cursor: str | None
    truncated: bool


def read_original_review_history(
    *,
    store: StackHistoryStore,
    repository: str,
    base_branch: str,
    trace_id: str,
    cursor: str = "",
    limit: int = 50,
) -> OriginalReviewHistoryResponse:
    # One scoped read covers active and retired rows without status-transition gaps.
    if not 1 <= limit <= 200:
        raise ValueError("History page size must be between 1 and 200.")
    groups: dict[str, list[MergeTrainStackCollapsePlanRecord]] = {}
    for stored in store.list_merge_train_stack_collapse_plan_records(
        repository=repository, base_branch=base_branch
    ):
        if stored.plan.repository == repository and stored.plan.base_branch == base_branch:
            groups.setdefault(stored.plan.collapse_id, []).append(stored)
    entries = []
    for collapse_id, group in sorted(groups.items()):
        record = latest_merge_train_stack_collapse_progress_record(tuple(group))
        if record is None:
            continue
        entries.append(
            OriginalReviewHistoryEntry(
                record_id=record.record_id,
                collapse_id=collapse_id,
                status=record.status,
                updated_at=record.updated_at,
                root_pull_request_number=record.plan.root_pull_request_number,
                root_head_ref=record.plan.root_head_ref,
                root_initial_head_sha=record.plan.root_initial_head_sha,
                original_changes=tuple(
                    OriginalReviewHistoryChange(
                        parent_pull_request_number=mutation.parent_pull_request_number,
                        parent_head_ref=mutation.parent_head_ref,
                        parent_initial_head_sha=mutation.expected_parent_head_sha,
                        child_pull_request_number=mutation.child_pull_request_number,
                        child_head_sha=mutation.child_head_sha,
                        merge_commit_sha=mutation.merge_commit_sha,
                        status=mutation.status,
                    )
                    for mutation in record.plan.mutations
                ),
            )
        )
    digest = hashlib.sha256(
        json.dumps([entry.model_dump(mode="json") for entry in entries], sort_keys=True).encode()
    ).hexdigest()
    remaining = [entry for entry in entries if entry.collapse_id > cursor]
    page = tuple(remaining[:limit])
    more = len(remaining) > limit
    return OriginalReviewHistoryResponse(
        trace_id=trace_id,
        repository=repository,
        base_branch=base_branch,
        history_digest=digest,
        entries=page,
        next_cursor=page[-1].collapse_id if more else None,
        truncated=more,
    )
