"""One durable reservation per repository/PR/head, using native feedback storage."""

from __future__ import annotations

from hashlib import sha256
from typing import cast

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainSetupFailureRecovery,
)
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.contracts.merge_train_pr_feedback_record import (
    MergeTrainPrFeedbackRecord,
    merge_train_pr_feedback_marker,
)
from control_plane.merge_train_pr_feedback import MergeTrainPrFeedbackRecordStore

_SOURCE = "launchplane:setup-recovery-reservation:v1"
_PREFIX = "<!-- launchplane-setup-recovery:v1 "


class SetupRecoveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    failed_record_id: str
    evidence: MergeTrainSetupFailureRecovery


def receipt_id(repository: str, number: int, head: str) -> str:
    key = f"{repository.casefold()}\0{number}\0{head}"
    return "setup-recovery-" + sha256(key.encode()).hexdigest()


def read_setup_recovery_receipt(
    store: object, repository: str, number: int, head: str
) -> SetupRecoveryReceipt | None:
    reader = getattr(store, "read_merge_train_pr_feedback_record", None)
    if not callable(reader):
        if store is None:
            return None
        raise ValueError("Setup recovery receipt storage is unavailable.")
    record = reader(receipt_id(repository, number, head))
    if record is None:
        return None
    if not isinstance(record, MergeTrainPrFeedbackRecord) or (
        record.repository.casefold(),
        record.pull_request_number,
        record.source,
        record.event,
        record.controller_action,
    ) != (repository.casefold(), number, _SOURCE, "blocked", "block"):
        raise ValueError("Setup recovery reservation identity is invalid.")
    _, marker, suffix = record.comment_markdown.partition(_PREFIX)
    if not marker or not suffix.endswith(" -->"):
        raise ValueError("Setup recovery reservation proof is invalid.")
    receipt = SetupRecoveryReceipt.model_validate_json(suffix[:-4])
    if receipt.evidence.head_sha != head or record.controller_record_id != receipt.failed_record_id:
        raise ValueError("Setup recovery reservation binding is invalid.")
    return receipt


def reserve_setup_recovery(
    *,
    store: object,
    candidate: MergeTrainBatchCandidateRecord,
    policy: MergeTrainRepositoryPolicy,
    evidence: MergeTrainSetupFailureRecovery,
    recorded_at: str,
) -> None:
    feedback_store = cast(MergeTrainPrFeedbackRecordStore, store)
    entry = candidate.candidate.entries[0]
    receipt = SetupRecoveryReceipt(failed_record_id=candidate.record_id, evidence=evidence)
    # Exit this lock before DELETE. PostgreSQL commits the receipt on exit;
    # a rollback or a failed commit therefore cannot erase an effected budget.
    with feedback_store.merge_train_feedback_delivery_lock(
        repository=policy.repository, pull_request_number=entry.pull_request_number
    ):
        existing = read_setup_recovery_receipt(
            store, policy.repository, entry.pull_request_number, entry.head_sha
        )
        if existing is not None:
            if existing != receipt:
                raise ValueError("Setup recovery budget is already reserved.")
            return
        feedback_store.write_merge_train_pr_feedback_record(
            MergeTrainPrFeedbackRecord(
                feedback_id=receipt_id(
                    policy.repository, entry.pull_request_number, entry.head_sha
                ),
                repository=policy.repository,
                base_branch=policy.base_branch,
                pull_request_number=entry.pull_request_number,
                pull_request_url=entry.url,
                # The applied hold is still blocked. This checkpoint preserves its
                # native failure provenance even for legacy latest-feedback readers.
                event="blocked",
                controller_action="block",
                controller_record_id=candidate.record_id,
                marker=merge_train_pr_feedback_marker(
                    repository=policy.repository,
                    base_branch=policy.base_branch,
                    pull_request_number=entry.pull_request_number,
                ),
                comment_markdown="Launchplane reserved one causal recovery of its existing block.\n\n"
                + _PREFIX
                + receipt.model_dump_json()
                + " -->",
                source=_SOURCE,
                recorded_at=recorded_at,
                policy_key=policy.policy_key,
                policy_sha256=candidate.candidate.policy_sha256,
                delivery_status="skipped",
                delivery_action="checkpoint",
                retryable=False,
            )
        )
