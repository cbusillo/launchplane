"""Keep a legacy carried change on the PR whose review belongs to it."""

from typing import Protocol

from control_plane.contracts.merge_train_stack_collapse import MergeTrainStackCollapsePlanRecord
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.workflows.merge_train_controller import (
    latest_merge_train_stack_collapse_progress_record,
)


class CommitAncestryReader(Protocol):
    def carried_change_requires_individual_landing(
        self,
        *,
        repository: str,
        base_sha: str,
        parent_head_sha: str,
        child_head_sha: str,
    ) -> bool: ...

    def branch_contains_commit(
        self, *, repository: str, branch_ref: str, commit_sha: str
    ) -> bool: ...


def original_review_recovery_records(
    *,
    records: tuple[MergeTrainStackCollapsePlanRecord, ...],
    snapshot: MergeTrainDryRunSnapshot,
    ancestry: CommitAncestryReader,
) -> tuple[MergeTrainStackCollapsePlanRecord, ...]:
    owned = {pr.number: pr for pr in snapshot.pull_requests if pr.requires_individual_landing}
    if not owned:
        return ()
    groups: dict[str, list[MergeTrainStackCollapsePlanRecord]] = {}
    for stored in records:
        if (
            stored.plan.repository != snapshot.repository
            or stored.plan.base_branch != snapshot.base_branch
        ):
            continue
        groups.setdefault(stored.plan.collapse_id, []).append(stored)
    cache: dict[tuple[str, str], bool] = {}

    def contains(head: str, child: str) -> bool:
        if not head or not child:
            raise ValueError("Original-review recovery requires exact commit evidence.")
        pair = (head, child)
        if pair not in cache:
            cache[pair] = ancestry.branch_contains_commit(
                repository=snapshot.repository, branch_ref=head, commit_sha=child
            )
        return cache[pair]

    blocked = []
    for group in groups.values():
        record = latest_merge_train_stack_collapse_progress_record(tuple(group))
        if record is None:
            continue
        for mutation in record.plan.mutations:
            parent = owned.get(mutation.parent_pull_request_number)
            child = mutation.child_head_sha
            if (
                parent is not None
                and contains(parent.head_sha, child)
                and not contains(snapshot.base_sha, child)
                and ancestry.carried_change_requires_individual_landing(
                    repository=snapshot.repository,
                    base_sha=snapshot.base_sha,
                    parent_head_sha=mutation.expected_parent_head_sha,
                    child_head_sha=child,
                )
            ):
                blocked.append(record)
                break
    return tuple(blocked)


ORIGINAL_REVIEW_RECOVERY_CODE = "merge_train_original_review_recovery_required"


def original_review_recovery_details(
    records: tuple[MergeTrainStackCollapsePlanRecord, ...],
) -> dict[str, object]:
    return {
        "code": ORIGINAL_REVIEW_RECOVERY_CODE,
        "record_id": records[0].record_id,
        "entries": [
            {
                "record_id": record.record_id,
                "root_pull_request_number": record.plan.root_pull_request_number,
                "root_initial_head_sha": record.plan.root_initial_head_sha,
                "original_changes": [
                    {
                        "parent_pull_request_number": mutation.parent_pull_request_number,
                        "parent_initial_head_sha": mutation.expected_parent_head_sha,
                        "child_pull_request_number": mutation.child_pull_request_number,
                        "child_head_sha": mutation.child_head_sha,
                    }
                    for mutation in record.plan.mutations
                ],
            }
            for record in sorted(records, key=lambda item: item.record_id)
        ],
    }


class StackHistoryStore(Protocol):
    def list_merge_train_stack_collapse_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainStackCollapsePlanRecord, ...]: ...
