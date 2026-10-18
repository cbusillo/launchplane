"""Recover only recorded failed-candidate holds, before a fresh ready handoff."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.contracts.merge_train_pr_feedback_record import MergeTrainPrFeedbackRecord
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_batch_candidate import MergeTrainBatchCandidateRecordStore
from control_plane.merge_train_pr_feedback import MergeTrainPrFeedbackRecordStore


class CandidateBlockRecoveryClient(Protocol):
    def recover_candidate_block(
        self,
        *,
        candidate: MergeTrainBatchCandidateRecord,
        feedback: MergeTrainPrFeedbackRecord,
        policy: MergeTrainRepositoryPolicy,
        expected_head_sha: str,
        mutate: bool,
        before_remove: Callable[[], None],
    ) -> bool: ...


def recover_candidate_block(
    *,
    snapshot: MergeTrainDryRunSnapshot,
    policy: MergeTrainRepositoryPolicy,
    policy_sha256: str,
    store: MergeTrainBatchCandidateRecordStore,
    client: CandidateBlockRecoveryClient,
    mutate: bool,
    before_remove: Callable[[str, int], None],
) -> dict[str, object] | None:
    blocked = {
        pr.number: pr
        for pr in snapshot.pull_requests
        if policy.blocked_label.casefold() in {label.casefold() for label in pr.labels}
    }
    if not blocked or not hasattr(store, "list_merge_train_pr_feedback_records"):
        return None
    feedback_store = cast(MergeTrainPrFeedbackRecordStore, store)
    # One delivered, service-owned receipt identifies the exact failed record;
    # generic labels and older unrelated failures never supply this proof.
    feedback_records = feedback_store.list_merge_train_pr_feedback_records(
        repository=snapshot.repository, base_branch=snapshot.base_branch, latest_per_pr=True
    )
    receipts = {
        record.controller_record_id: record
        for record in feedback_records
        if record.pull_request_number in blocked
        and record.event == "blocked"
        and record.controller_action == "block"
        and record.source == "launchplane:merge-train-scheduler"
        and record.delivery_status == "delivered"
        and record.comment_id > 0
        and record.policy_key == policy.policy_key
        and record.policy_sha256 == policy_sha256
    }
    if not receipts:
        return None
    for candidate in store.list_merge_train_batch_candidate_records(
        repository=snapshot.repository, base_branch=snapshot.base_branch, status="superseded"
    ):
        feedback = receipts.get(candidate.record_id)
        failed = candidate.candidate
        if (
            feedback is None
            or candidate.ordinary_job_binding is not None
            or not candidate.source.startswith("service:controller:")
            or failed.status != "failed"
            or failed.required_checks_status != "fail"
            or failed.repository != snapshot.repository
            or failed.base_branch != snapshot.base_branch
            or failed.policy_key != policy.policy_key
            or failed.policy_sha256 != policy_sha256
            or len(failed.entries) != 1
        ):
            continue
        entry = failed.entries[0]
        if entry.pull_request_number != feedback.pull_request_number:
            continue
        current = blocked[entry.pull_request_number]
        if current.head_sha == entry.head_sha:
            continue
        if client.recover_candidate_block(
            candidate=candidate,
            feedback=feedback,
            policy=policy,
            expected_head_sha=current.head_sha,
            mutate=mutate,
            before_remove=lambda: before_remove(candidate.record_id, entry.pull_request_number),
        ):
            return {
                "repository": snapshot.repository,
                "base_branch": snapshot.base_branch,
                "mode": "recover_candidate_block" if mutate else "dry-run",
                "controller_action": "recover_candidate_block",
                "reason_code": "candidate_block_new_head_reviewed",
                "merge_train_batch_candidate_record_id": candidate.record_id,
                "details": {
                    "status": "block_removed" if mutate else "recovery_available",
                    "pull_request_number": entry.pull_request_number,
                    "blocked_head_sha": entry.head_sha,
                    "head_sha": current.head_sha,
                    "source_failed_record_id": candidate.record_id,
                    "block_feedback_id": feedback.feedback_id,
                    "block_removed": mutate,
                    "next_action": "Post a fresh checked gh-pr.py ready handoff for the current head.",
                },
            }
    return None
