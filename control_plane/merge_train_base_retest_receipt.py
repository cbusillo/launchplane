from __future__ import annotations

from hashlib import sha256
from typing import cast

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.contracts.merge_train_pr_feedback_record import (
    MergeTrainPrFeedbackRecord,
    merge_train_pr_feedback_marker,
)
from control_plane.merge_train_pr_feedback import MergeTrainPrFeedbackRecordStore

_SOURCE = "launchplane:base-retest-reservation:v1"
_PREFIX = "<!-- launchplane-base-retest:v1 "


class BaseRetestRequiredCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str = Field(min_length=1)
    app_id: int | None = Field(default=None, ge=1)


class BaseRetestReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    failed_record_id: str
    failed_base_sha: str = Field(min_length=1)
    retest_base_sha: str = Field(min_length=1)
    required_checks: tuple[BaseRetestRequiredCheck, ...] = Field(min_length=1)


def receipt_id(repository: str, number: int, head: str) -> str:
    key = f"{repository.casefold()}\0{number}\0{head}"
    return "base-retest-" + sha256(key.encode()).hexdigest()


def read_base_retest_receipt(
    store: object, repository: str, number: int, head: str
) -> BaseRetestReceipt | None:
    reader = getattr(store, "read_merge_train_pr_feedback_record", None)
    if reader is None or not callable(reader):
        if store is None:
            return None
        raise ValueError("Base retest receipt storage is unavailable.")
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
        raise ValueError("Base retest reservation identity is invalid.")
    _, marker, suffix = record.comment_markdown.partition(_PREFIX)
    if not marker or not suffix.endswith(" -->"):
        raise ValueError("Base retest reservation proof is invalid.")
    receipt = BaseRetestReceipt.model_validate_json(suffix[:-4])
    if record.controller_record_id != receipt.failed_record_id:
        raise ValueError("Base retest reservation binding is invalid.")
    return receipt


def reserve_base_retest(
    *,
    store: object,
    candidate: MergeTrainBatchCandidateRecord,
    policy: MergeTrainRepositoryPolicy,
    receipt: BaseRetestReceipt,
    recorded_at: str,
) -> None:
    feedback_store = cast(MergeTrainPrFeedbackRecordStore, store)
    entry = candidate.candidate.entries[0]
    with feedback_store.merge_train_feedback_delivery_lock(
        repository=policy.repository, pull_request_number=entry.pull_request_number
    ):
        existing = read_base_retest_receipt(
            store, policy.repository, entry.pull_request_number, entry.head_sha
        )
        if existing is not None:
            if existing != receipt:
                raise ValueError("Base retest budget is already reserved.")
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
                event="blocked",
                controller_action="block",
                controller_record_id=candidate.record_id,
                marker=merge_train_pr_feedback_marker(
                    repository=policy.repository,
                    base_branch=policy.base_branch,
                    pull_request_number=entry.pull_request_number,
                ),
                comment_markdown="Launchplane reserved one unchanged-head retest on a new base.\n\n"
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
