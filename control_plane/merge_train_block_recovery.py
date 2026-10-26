"""Recover only recorded failed-candidate holds, before a fresh ready handoff."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol, cast

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainSetupFailureRecovery,
)
from control_plane.contracts.merge_train_policy import (
    MergeTrainPolicyRecord,
    MergeTrainRepositoryPolicy,
)
from control_plane.contracts.merge_train_pr_feedback_record import MergeTrainPrFeedbackRecord
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_batch_candidate import MergeTrainBatchCandidateRecordStore
from control_plane.merge_train_pr_feedback import MergeTrainPrFeedbackRecordStore
from control_plane.merge_train_setup_recovery_receipt import (
    read_setup_recovery_receipt,
    reserve_setup_recovery,
    receipt_id,
    SetupRecoveryReceipt,
)

_LOGGER = logging.getLogger(__name__)


class CandidateBlockRecoveryClient(Protocol):
    def read_setup_failure_recovery(
        self, *, candidate: MergeTrainBatchCandidate
    ) -> MergeTrainSetupFailureRecovery | None: ...

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
        setup_failure_evidence: Callable[[], MergeTrainSetupFailureRecovery | None] | None = None,
    ) -> bool: ...


def recover_candidate_block(
    *,
    snapshot: MergeTrainDryRunSnapshot,
    policy: MergeTrainRepositoryPolicy,
    store: MergeTrainBatchCandidateRecordStore,
    client: CandidateBlockRecoveryClient,
    mutate: bool,
    before_remove: Callable[[dict[str, object]], None],
    recorded_at: str,
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
    reservation_cache: dict[tuple[int, str], SetupRecoveryReceipt | None] = {}

    def reservation_for(number: int, head: str) -> SetupRecoveryReceipt | None:
        key = (number, head)
        if key not in reservation_cache:
            reservation_cache[key] = read_setup_recovery_receipt(
                store, snapshot.repository, number, head
            )
        return reservation_cache[key]

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
            candidate.ordinary_job_binding is not None
            or not candidate.source.startswith("service:controller:")
            or failed.status != "failed"
            or failed.required_checks_status != "fail"
            or len(failed.entries) != 1
        ):
            continue
        # Candidate records store the repository lowercased; the snapshot keeps
        # the caller's casing. GitHub repository names are case-insensitive.
        if (
            failed.repository.casefold() != snapshot.repository.casefold()
            or failed.base_branch != snapshot.base_branch
            or failed.policy_key != policy.policy_key
        ):
            _LOGGER.warning(
                "Candidate block recovery skipped a candidate for another target; preserving hold",
                extra={
                    "repository": snapshot.repository,
                    "base_branch": snapshot.base_branch,
                    "policy_key": policy.policy_key,
                    "merge_train_batch_candidate_record_id": candidate.record_id,
                    "candidate_repository": failed.repository,
                    "candidate_base_branch": failed.base_branch,
                    "candidate_policy_key": failed.policy_key,
                },
            )
            continue
        entry = failed.entries[0]
        if entry.pull_request_number not in blocked or (
            feedback is not None and entry.pull_request_number != feedback.pull_request_number
        ):
            continue
        if feedback is None and candidate.block_intent is None:
            # Later feedback can supersede the latest blocked projection. The
            # exact committed reservation retains this legacy decision itself.
            try:
                saved = reservation_for(entry.pull_request_number, entry.head_sha)
                if saved is not None and saved.failed_record_id == candidate.record_id:
                    feedback = feedback_store.read_merge_train_pr_feedback_record(
                        receipt_id(snapshot.repository, entry.pull_request_number, entry.head_sha)
                    )
            except (AttributeError, OSError, ValueError, TypeError, KeyError):
                continue
        if feedback is None and candidate.block_intent is None:
            continue
        current = blocked[entry.pull_request_number]
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
        setup_failure_evidence = None
        reservation = None
        if current.head_sha == entry.head_sha:
            if (
                current.required_checks_status != "pass"
                or current.is_draft
                or current.mergeable != "mergeable"
            ):
                continue
            if (
                failed.infrastructure_retry_of
                or failed.check_retry_of
                or failed.batch_body_retry_of
            ):
                continue
            try:
                reservation = reservation_for(entry.pull_request_number, entry.head_sha)
                if reservation is not None and reservation.failed_record_id != candidate.record_id:
                    continue
            except (OSError, ValueError, TypeError, KeyError):
                continue

        def load_setup_evidence() -> MergeTrainSetupFailureRecovery | None:
            nonlocal setup_failure_evidence
            evidence = client.read_setup_failure_recovery(candidate=failed)
            if evidence is None:
                return None
            if reservation is not None:
                evidence = evidence.model_copy(
                    update={"ready_comment_floor": reservation.evidence.ready_comment_floor}
                )
                if reservation.evidence != evidence:
                    return None
            setup_failure_evidence = evidence
            cast(dict[str, object], result["details"])["setup_failure_evidence"] = (
                evidence.model_dump(mode="json")
            )
            return evidence

        result: dict[str, object] = {
            "repository": snapshot.repository,
            "base_branch": snapshot.base_branch,
            "mode": "recover_candidate_block" if mutate else "dry-run",
            "controller_action": "recover_candidate_block",
            "reason_code": (
                "candidate_block_unrelated_setup_reviewed"
                if current.head_sha == entry.head_sha
                else "candidate_block_new_head_reviewed"
            ),
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

        def checkpoint() -> None:
            before_remove(result)
            if setup_failure_evidence is not None:
                reserve_setup_recovery(
                    store=store,
                    candidate=candidate,
                    policy=policy,
                    evidence=setup_failure_evidence,
                    recorded_at=recorded_at,
                )
                reservation_cache[(entry.pull_request_number, entry.head_sha)] = (
                    SetupRecoveryReceipt(
                        failed_record_id=candidate.record_id, evidence=setup_failure_evidence
                    )
                )

        if client.recover_candidate_block(
            candidate=candidate,
            feedback=feedback,
            policy=policy,
            expected_head_sha=current.head_sha,
            legacy_writer_app_id=legacy_writer_app_id,
            mutate=mutate,
            before_remove=checkpoint,
            setup_failure_evidence=(
                load_setup_evidence if current.head_sha == entry.head_sha else None
            ),
        ):
            return result
    return None
