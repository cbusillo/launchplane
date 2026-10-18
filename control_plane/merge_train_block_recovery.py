"""Recover only recorded failed-candidate holds, before a fresh ready handoff."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    MergeTrainRepositoryPolicy,
)
from control_plane.contracts.merge_train_pr_feedback_record import MergeTrainPrFeedbackRecord
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_batch_candidate import MergeTrainBatchCandidateRecordStore
from control_plane.merge_train_pr_feedback import MergeTrainPrFeedbackRecordStore


class CandidateBlockRecoveryClient(Protocol):
    def recover_candidate_block(
        self,
        *,
        candidate: MergeTrainBatchCandidateRecord,
        feedback: MergeTrainPrFeedbackRecord | None,
        policy: MergeTrainRepositoryPolicy,
        expected_head_sha: str,
        legacy_writer_app_id: int | None,
        mutate: bool,
        before_remove: Callable[[], None],
    ) -> bool: ...


def recover_candidate_block(
    *,
    snapshot: MergeTrainDryRunSnapshot,
    policy: MergeTrainRepositoryPolicy,
    store: MergeTrainBatchCandidateRecordStore,
    client: CandidateBlockRecoveryClient,
    mutate: bool,
    before_remove: Callable[[dict[str, object]], None],
) -> dict[str, object] | None:
    blocked = {
        pr.number: pr
        for pr in snapshot.pull_requests
        if policy.blocked_label.casefold() in {label.casefold() for label in pr.labels}
    }
    if not blocked:
        return None
    feedback_store = cast(MergeTrainPrFeedbackRecordStore, store)
    # New blocks carry a persisted decision. Legacy controller outcome receipts
    # can identify the exact failure even when comment delivery was deferred.
    feedback_records = (
        feedback_store.list_merge_train_pr_feedback_records(
            repository=snapshot.repository, base_branch=snapshot.base_branch, latest_per_pr=True
        )
        if hasattr(store, "list_merge_train_pr_feedback_records")
        else ()
    )
    receipts = {
        record.controller_record_id: record
        for record in feedback_records
        if record.pull_request_number in blocked
        and record.event == "blocked"
        and record.controller_action == "block"
        and record.policy_key == policy.policy_key
    }
    candidates = store.list_merge_train_batch_candidate_records(
        repository=snapshot.repository, base_branch=snapshot.base_branch, status="superseded"
    )
    recorded_policies: tuple[MergeTrainPolicyRecord, ...] | None = None
    explicit_batches = {
        record.candidate.batch_id for record in candidates if record.block_intent is not None
    }
    # Prefer the explicit decision over legacy reconstruction, regardless of
    # service clock ordering or feedback delivery chronology.
    for candidate in sorted(
        candidates, key=lambda record: record.block_intent is not None, reverse=True
    ):
        feedback = receipts.get(candidate.record_id)
        failed = candidate.candidate
        if candidate.block_intent is None and failed.batch_id in explicit_batches:
            # A saved decision owns this failure's provenance. An older
            # generic outcome must not replace its missing current event proof.
            continue
        if (
            candidate.block_intent is not None
            and candidate.block_intent.applied_label_event_id is None
            and not candidate.block_intent.label_write_succeeded
        ):
            continue
        if (
            (feedback is None and candidate.block_intent is None)
            or candidate.ordinary_job_binding is not None
            or not candidate.source.startswith("service:controller:")
            or failed.status != "failed"
            or failed.required_checks_status != "fail"
            or failed.repository != snapshot.repository
            or failed.base_branch != snapshot.base_branch
            or failed.policy_key != policy.policy_key
            or len(failed.entries) != 1
        ):
            continue
        entry = failed.entries[0]
        if entry.pull_request_number not in blocked or (
            feedback is not None and entry.pull_request_number != feedback.pull_request_number
        ):
            continue
        current = blocked[entry.pull_request_number]
        if current.head_sha == entry.head_sha:
            continue
        legacy_writer_app_id = None
        if candidate.block_intent is None:
            # A legacy failure did not save its label name or writer App. Read
            # that source policy once per pass, without reusing its admission
            # authority. Only target/label provenance is needed to release a
            # hold; the fresh Ready path still enforces today's full policy.
            if recorded_policies is None:
                reader = getattr(store, "list_merge_train_policy_records", None)
                recorded_policies = (
                    tuple(MergeTrainPolicyRecord.model_validate(row) for row in reader())
                    if callable(reader)
                    else ()
                )
            targets = [
                target
                for row in recorded_policies
                if row.policy_sha256 == failed.policy_sha256
                for target in row.policy.policies
                if target.policy_key == policy.policy_key
            ]
            if not targets or any(
                target.blocked_label.casefold() != policy.blocked_label.casefold()
                or target.github_token != targets[0].github_token
                for target in targets
            ):
                continue
            source_app = targets[0].github_token.github_app
            legacy_writer_app_id = source_app.app_id if source_app is not None else None
        result: dict[str, object] = {
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
                "source_failed_record_id": (
                    candidate.block_intent.source_failed_record_id
                    if candidate.block_intent
                    else candidate.record_id
                ),
                "block_feedback_id": feedback.feedback_id if feedback is not None else "",
                "block_removed": mutate,
                "next_action": "Post a fresh checked gh-pr.py ready handoff for the current head.",
            },
        }
        if client.recover_candidate_block(
            candidate=candidate,
            feedback=feedback,
            policy=policy,
            expected_head_sha=current.head_sha,
            legacy_writer_app_id=legacy_writer_app_id,
            mutate=mutate,
            before_remove=lambda: before_remove(result),
        ):
            return result
    return None
