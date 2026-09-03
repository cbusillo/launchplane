from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Protocol, cast

from control_plane.contracts.privileged_operation import (
    ManagedAuthzPolicySetHumanEvidence,
    ManagedAuthzPolicySetProposalInput,
    ManagedMergeTrainPolicyImportHumanEvidence,
    ManagedMergeTrainPolicyImportProposalInput,
    ManagedSecretReencryptionPlanInput,
    ManagedSecretReencryptionHumanEvidence,
    PrivilegedOperationActor,
    PrivilegedOperationApproval,
    PrivilegedOperationConflictError,
    PrivilegedOperationSemanticReview,
    PrivilegedOperationSemanticReviewActivityEntry,
    PrivilegedOperationSemanticReviewBlastRadius,
    PrivilegedOperationSemanticReviewBlocker,
    PrivilegedOperationSemanticReviewBlockerCode,
    PrivilegedOperationSemanticReviewChange,
    PrivilegedOperationSemanticReviewDigest,
    PrivilegedOperationSemanticReviewEvidence,
    PrivilegedOperationSemanticReviewLifecycle,
    PrivilegedOperationSemanticReviewMetric,
    PrivilegedOperationSemanticReviewRollback,
    PrivilegedOperationEventRecord,
    PrivilegedOperationEventWriteStatus,
    PrivilegedOperationDescriptorId,
    PrivilegedOperationRecord,
    PrivilegedOperationRequest,
    PrivilegedOperationRequester,
    PrivilegedOperationSourceKind,
    build_privileged_operation_event_id,
    build_privileged_operation_id,
    build_privileged_operation_id_for_actor,
    privileged_operation_evidence_digest,
    privileged_operation_record_digest,
    privileged_operation_pre_state_digest,
    privileged_operation_request_digest,
    privileged_operation_request_digest_candidates,
)
from control_plane.privileged_operation_registry import (
    list_privileged_operation_descriptors,
    read_privileged_operation_descriptor,
)


DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS = 30 * 60
MIN_PRIVILEGED_OPERATION_TTL_SECONDS = 5 * 60
MAX_PRIVILEGED_OPERATION_TTL_SECONDS = 24 * 60 * 60


class PrivilegedOperationStore(Protocol):
    def write_privileged_operation_plan(
        self,
        record: PrivilegedOperationRecord,
        event: PrivilegedOperationEventRecord,
    ) -> PrivilegedOperationEventWriteStatus: ...

    def transition_privileged_operation(
        self,
        record: PrivilegedOperationRecord,
        event: PrivilegedOperationEventRecord,
    ) -> PrivilegedOperationEventWriteStatus: ...

    def read_privileged_operation_record(
        self,
        operation_id: str,
    ) -> PrivilegedOperationRecord: ...

    def list_privileged_operation_records(
        self,
        *,
        status: str = "",
        descriptor_id: str = "",
        limit: int | None = None,
    ) -> tuple[PrivilegedOperationRecord, ...]: ...

    def list_privileged_operation_event_records(
        self,
        *,
        operation_id: str = "",
        limit: int | None = None,
    ) -> tuple[PrivilegedOperationEventRecord, ...]: ...


class PrivilegedOperationStoreUnavailableError(TypeError):
    pass


class PrivilegedOperationNotCancellableError(ValueError):
    pass


class PrivilegedOperationNotApprovableError(ValueError):
    pass


class PrivilegedOperationNotRevocableError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PrivilegedOperationWriteResult:
    write_status: PrivilegedOperationEventWriteStatus
    record: PrivilegedOperationRecord
    event: PrivilegedOperationEventRecord


class PrivilegedOperationSemanticReviewError(ValueError):
    pass


_SEMANTIC_REVIEW_DESCRIPTOR_IDS = frozenset(
    {
        "managed-secret-reencryption",
        "managed-authz-policy-set",
        "managed-merge-train-policy-import",
    }
)


def validate_privileged_operation_semantic_review_coverage() -> None:
    registered_ids = frozenset(
        descriptor.descriptor_id for descriptor in list_privileged_operation_descriptors()
    )
    if registered_ids != _SEMANTIC_REVIEW_DESCRIPTOR_IDS:
        raise RuntimeError(
            "Privileged-operation semantic review coverage must exactly match the descriptor registry."
        )


def require_privileged_operation_store(record_store: object) -> PrivilegedOperationStore:
    required_methods = (
        "write_privileged_operation_plan",
        "transition_privileged_operation",
        "read_privileged_operation_record",
        "list_privileged_operation_records",
        "list_privileged_operation_event_records",
    )
    if any(
        not callable(getattr(record_store, method_name, None)) for method_name in required_methods
    ):
        raise PrivilegedOperationStoreUnavailableError(
            "Privileged-operation planning requires operation record and event storage."
        )
    return cast(PrivilegedOperationStore, record_store)


def _semantic_review_activity(
    operation_id: str,
    events: tuple[PrivilegedOperationEventRecord, ...],
) -> tuple[PrivilegedOperationSemanticReviewActivityEntry, ...]:
    try:
        validated_events = tuple(
            PrivilegedOperationEventRecord.model_validate(event.model_dump(mode="json"))
            for event in events
        )
    except ValueError as error:
        raise PrivilegedOperationSemanticReviewError(
            "Privileged-operation semantic review activity contains an invalid event."
        ) from error
    if any(event.operation_id != operation_id for event in validated_events):
        raise PrivilegedOperationSemanticReviewError(
            "Privileged-operation semantic review activity crossed operation boundaries."
        )
    if len({event.event_id for event in validated_events}) != len(validated_events) or len(
        {event.sequence for event in validated_events}
    ) != len(validated_events):
        raise PrivilegedOperationSemanticReviewError(
            "Privileged-operation semantic review activity is not append-only unique."
        )
    ordered_events = sorted(
        validated_events,
        key=lambda event: (event.sequence, event.occurred_at, event.event_id),
    )
    return tuple(
        PrivilegedOperationSemanticReviewActivityEntry(
            sequence=event.sequence,
            action=event.action,
            occurred_at=event.occurred_at,
            source_kind=event.source_kind,
            actor_type=event.actor.identity_type,
            reason_available=bool(event.reason),
            event_id=event.event_id,
            resulting_record_digest=event.resulting_record_digest,
        )
        for event in ordered_events
    )


def _semantic_review_requester_kind(
    record: PrivilegedOperationRecord,
) -> Literal["github_human", "terminal_agent"]:
    if record.requested_by.identity_type in {"github_human", "terminal_agent"}:
        return record.requested_by.identity_type
    raise PrivilegedOperationSemanticReviewError(
        "Privileged-operation semantic review requester variant drifted."
    )


def _semantic_review_lifecycle(
    record: PrivilegedOperationRecord,
    *,
    generated_at: datetime,
) -> PrivilegedOperationSemanticReviewLifecycle:
    expires_at = datetime.fromisoformat(record.expires_at)
    if record.status == "expired":
        expiry_state: Literal["active", "past_expiry_unreconciled", "expired"] = "expired"
    elif record.status in {"planned", "approved"} and generated_at >= expires_at:
        expiry_state = "past_expiry_unreconciled"
    else:
        expiry_state = "active"
    return PrivilegedOperationSemanticReviewLifecycle(
        status=record.status,
        generated_at=_timestamp(generated_at),
        expiry_state=expiry_state,
        created_at=record.created_at,
        updated_at=record.updated_at,
        expires_at=record.expires_at,
        terminal_at=record.terminal_at,
        terminal_reason_available=bool(record.terminal_reason),
        approval_recorded=record.approval is not None,
        execution_recorded=record.execution is not None,
    )


def _semantic_review_digests(
    record: PrivilegedOperationRecord,
    extra: tuple[PrivilegedOperationSemanticReviewDigest, ...] = (),
) -> tuple[PrivilegedOperationSemanticReviewDigest, ...]:
    digests: tuple[PrivilegedOperationSemanticReviewDigest, ...] = (
        PrivilegedOperationSemanticReviewDigest(
            kind="request",
            label="Request digest",
            sha256=record.request_digest,
        ),
        PrivilegedOperationSemanticReviewDigest(
            kind="human_evidence",
            label="Human evidence digest",
            sha256=record.evidence_digest,
        ),
        PrivilegedOperationSemanticReviewDigest(
            kind="plan",
            label="Plan digest",
            sha256=record.evidence.plan_digest,
        ),
        PrivilegedOperationSemanticReviewDigest(
            kind="pre_state",
            label="Pre-state digest",
            sha256=privileged_operation_pre_state_digest(record.evidence),
        ),
    )
    execution = record.execution
    if execution is not None:
        digests += (
            PrivilegedOperationSemanticReviewDigest(
                kind="execution_result",
                label="Execution result digest",
                sha256=execution.result_digest,
            ),
        )
    return digests + extra


def _semantic_review_result_status(
    record: PrivilegedOperationRecord,
) -> Literal["ok", "blocked", "error"]:
    if record.execution is not None and record.execution.result_status == "error":
        return "error"
    return record.evidence.result_status


def _semantic_review_lifecycle_blocker_codes(
    record: PrivilegedOperationRecord,
    *,
    expiry_state: Literal["active", "past_expiry_unreconciled", "expired"],
) -> tuple[PrivilegedOperationSemanticReviewBlockerCode, ...]:
    codes: list[PrivilegedOperationSemanticReviewBlockerCode] = []
    if expiry_state == "past_expiry_unreconciled":
        codes.append("operation_past_expiry")
    elif expiry_state == "expired":
        codes.append("operation_expired")
    if record.status == "execution_failed":
        codes.append("execution_failed")
    if record.execution is not None and record.execution.reconciliation_required:
        codes.append("reconciliation_required")
    return tuple(codes)


def privileged_operation_semantic_review(
    *,
    record: PrivilegedOperationRecord,
    events: tuple[PrivilegedOperationEventRecord, ...] = (),
    generated_at: datetime | None = None,
) -> PrivilegedOperationSemanticReview:
    observed_at = (generated_at or _utc_now()).astimezone(timezone.utc)
    try:
        descriptor = read_privileged_operation_descriptor(record.descriptor_id).descriptor
    except LookupError as error:
        raise PrivilegedOperationSemanticReviewError(
            "Privileged-operation semantic review descriptor is not registered."
        ) from error
    if (
        descriptor.descriptor_version != record.descriptor_version
        or descriptor.safety_class != record.safety_class
    ):
        raise PrivilegedOperationSemanticReviewError(
            "Privileged-operation semantic review descriptor metadata drifted."
        )
    if record.descriptor_id == "managed-secret-reencryption":
        if not isinstance(record.request, ManagedSecretReencryptionPlanInput) or not isinstance(
            record.evidence,
            ManagedSecretReencryptionHumanEvidence,
        ):
            raise PrivilegedOperationSemanticReviewError(
                "Managed-secret semantic review payload variant drifted."
            )
        secret_metrics: tuple[PrivilegedOperationSemanticReviewMetric, ...] = (
            PrivilegedOperationSemanticReviewMetric(
                kind="configured_secrets",
                label="Configured secrets",
                value=record.evidence.configured_secret_count,
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="rotation_candidates",
                label="Would rotate",
                value=record.evidence.rotation_candidate_count,
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="unchanged_secrets",
                label="Unchanged",
                value=record.evidence.unchanged_count,
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="unreadable_secrets",
                label="Unreadable",
                value=record.evidence.unreadable_secret_count,
            ),
        )
        lifecycle = _semantic_review_lifecycle(record, generated_at=observed_at)
        secret_blocker_codes = _semantic_review_lifecycle_blocker_codes(
            record,
            expiry_state=lifecycle.expiry_state,
        )
        if record.evidence.unreadable_secret_count:
            secret_blocker_codes += ("secret_unreadable",)
        blocker_state: Literal["clear", "blocked", "error"] = "clear"
        if (
            "execution_failed" in secret_blocker_codes
            or "reconciliation_required" in secret_blocker_codes
        ):
            blocker_state = "error"
        elif secret_blocker_codes:
            blocker_state = "blocked"
        return PrivilegedOperationSemanticReview(
            operation_id=record.operation_id,
            descriptor_id=record.descriptor_id,
            descriptor_version=record.descriptor_version,
            operation_class="managed_secret_reencryption",
            safety_class=record.safety_class,
            title="Managed-secret re-encryption review",
            requested_by_kind=_semantic_review_requester_kind(record),
            lifecycle=lifecycle,
            blockers=PrivilegedOperationSemanticReviewBlocker(
                state=blocker_state,
                unreadable_secret_count=record.evidence.unreadable_secret_count,
                codes=secret_blocker_codes,
            ),
            change=PrivilegedOperationSemanticReviewChange(
                summary="Managed secrets will be re-encrypted with the active key.",
                changed=record.evidence.rotation_candidate_count > 0,
                metrics=secret_metrics,
            ),
            blast_radius=PrivilegedOperationSemanticReviewBlastRadius(
                scope="managed_secret_store",
                summary="Bounded to configured managed-secret records.",
                affected_count=record.evidence.configured_secret_count,
            ),
            rollback=PrivilegedOperationSemanticReviewRollback(
                rollback_class="key_retained",
                summary="Rollback depends on retained managed-secret key material.",
            ),
            evidence=PrivilegedOperationSemanticReviewEvidence(
                result_status=_semantic_review_result_status(record),
                digests=_semantic_review_digests(record),
            ),
            activity=_semantic_review_activity(record.operation_id, events),
            can_approve=record.status == "planned" and not secret_blocker_codes,
            can_revoke=record.status == "approved" and lifecycle.expiry_state == "active",
        )
    if record.descriptor_id == "managed-authz-policy-set":
        if not isinstance(record.request, ManagedAuthzPolicySetProposalInput) or not isinstance(
            record.evidence,
            ManagedAuthzPolicySetHumanEvidence,
        ):
            raise PrivilegedOperationSemanticReviewError(
                "Managed-policy semantic review payload variant drifted."
            )
        diff = record.evidence.diff
        lifecycle = _semantic_review_lifecycle(record, generated_at=observed_at)
        authz_blocker_codes = cast(
            tuple[PrivilegedOperationSemanticReviewBlockerCode, ...],
            tuple(
                sorted(
                    {
                        *(blocker.code for blocker in diff.policy_safety_blockers),
                        *(
                            reason_code
                            for blocker in diff.operational_readiness_blockers
                            for reason_code in blocker.reason_codes
                        ),
                    }
                )
            ),
        )
        authz_blocker_codes += _semantic_review_lifecycle_blocker_codes(
            record,
            expiry_state=lifecycle.expiry_state,
        )
        authz_policy_metrics: tuple[PrivilegedOperationSemanticReviewMetric, ...] = (
            PrivilegedOperationSemanticReviewMetric(
                kind="policy_rules_added", label="Added", value=diff.added_rule_count
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="policy_rules_adopted", label="Adopted", value=diff.adopted_rule_count
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="policy_rules_updated", label="Updated", value=diff.updated_rule_count
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="policy_rules_removed", label="Removed", value=diff.removed_rule_count
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="policy_rules_unchanged", label="Unchanged", value=diff.unchanged_rule_count
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="policy_safety_blockers",
                label="Safety blockers",
                value=diff.policy_safety_blocker_count,
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="operational_readiness_blockers",
                label="Readiness blockers",
                value=diff.operational_readiness_blocked_rule_count,
            ),
        )
        return PrivilegedOperationSemanticReview(
            operation_id=record.operation_id,
            descriptor_id=record.descriptor_id,
            descriptor_version=record.descriptor_version,
            operation_class="managed_authz_policy_set",
            safety_class=record.safety_class,
            title="Managed authorization policy review",
            requested_by_kind=_semantic_review_requester_kind(record),
            lifecycle=lifecycle,
            blockers=PrivilegedOperationSemanticReviewBlocker(
                state=(
                    "error"
                    if "execution_failed" in authz_blocker_codes
                    or "reconciliation_required" in authz_blocker_codes
                    else "blocked"
                    if authz_blocker_codes
                    else "clear"
                ),
                policy_safety_blocker_count=diff.policy_safety_blocker_count,
                operational_readiness_blocker_count=(diff.operational_readiness_blocked_rule_count),
                codes=authz_blocker_codes,
            ),
            change=PrivilegedOperationSemanticReviewChange(
                summary="Managed authorization rules would be reconciled by exact policy CAS.",
                changed=diff.changed,
                metrics=authz_policy_metrics,
            ),
            blast_radius=PrivilegedOperationSemanticReviewBlastRadius(
                scope="authorization_policy",
                summary="Bounded to one managed authorization rule set.",
                affected_count=(
                    diff.added_rule_count
                    + diff.adopted_rule_count
                    + diff.updated_rule_count
                    + diff.removed_rule_count
                    + diff.unchanged_rule_count
                ),
            ),
            rollback=PrivilegedOperationSemanticReviewRollback(
                rollback_class="policy_cas",
                summary="Rollback is bounded by authorization policy CAS and record digest evidence.",
            ),
            evidence=PrivilegedOperationSemanticReviewEvidence(
                result_status=_semantic_review_result_status(record),
                digests=_semantic_review_digests(
                    record,
                    (
                        PrivilegedOperationSemanticReviewDigest(
                            kind="previous_policy",
                            label="Previous policy digest",
                            sha256=diff.previous_policy_sha256,
                        ),
                        PrivilegedOperationSemanticReviewDigest(
                            kind="candidate_policy",
                            label="Candidate policy digest",
                            sha256=diff.desired_policy_sha256,
                        ),
                        PrivilegedOperationSemanticReviewDigest(
                            kind="candidate_managed_set",
                            label="Candidate managed-set digest",
                            sha256=diff.desired_set_sha256,
                        ),
                    ),
                ),
            ),
            activity=_semantic_review_activity(record.operation_id, events),
            can_approve=record.status == "planned" and not authz_blocker_codes,
            can_revoke=record.status == "approved" and lifecycle.expiry_state == "active",
        )
    if record.descriptor_id == "managed-merge-train-policy-import":
        if not isinstance(
            record.request,
            ManagedMergeTrainPolicyImportProposalInput,
        ) or not isinstance(record.evidence, ManagedMergeTrainPolicyImportHumanEvidence):
            raise PrivilegedOperationSemanticReviewError(
                "Merge-train policy semantic review payload variant drifted."
            )
        merge_train_policy_metrics: tuple[PrivilegedOperationSemanticReviewMetric, ...] = (
            PrivilegedOperationSemanticReviewMetric(
                kind="active_policy_targets",
                label="Active targets",
                value=record.evidence.active_target_count,
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="candidate_policy_targets",
                label="Candidate targets",
                value=record.evidence.candidate_target_count,
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="policy_targets_added",
                label="Added",
                value=len(record.evidence.added_policy_keys),
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="policy_targets_changed",
                label="Changed",
                value=len(record.evidence.changed_policy_keys),
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="policy_targets_removed",
                label="Removed",
                value=len(record.evidence.removed_policy_keys),
            ),
            PrivilegedOperationSemanticReviewMetric(
                kind="policy_targets_unchanged",
                label="Unchanged",
                value=len(record.evidence.unchanged_policy_keys),
            ),
        )
        lifecycle = _semantic_review_lifecycle(record, generated_at=observed_at)
        merge_train_blocker_codes = _semantic_review_lifecycle_blocker_codes(
            record,
            expiry_state=lifecycle.expiry_state,
        )
        return PrivilegedOperationSemanticReview(
            operation_id=record.operation_id,
            descriptor_id=record.descriptor_id,
            descriptor_version=record.descriptor_version,
            operation_class="managed_merge_train_policy_import",
            safety_class=record.safety_class,
            title="Managed merge-train policy review",
            requested_by_kind=_semantic_review_requester_kind(record),
            lifecycle=lifecycle,
            blockers=PrivilegedOperationSemanticReviewBlocker(
                state=(
                    "error"
                    if "execution_failed" in merge_train_blocker_codes
                    or "reconciliation_required" in merge_train_blocker_codes
                    else "blocked"
                    if merge_train_blocker_codes
                    else "clear"
                ),
                codes=merge_train_blocker_codes,
            ),
            change=PrivilegedOperationSemanticReviewChange(
                summary="Merge-train policy targets would be imported by exact policy CAS.",
                changed=bool(
                    record.evidence.added_policy_keys
                    or record.evidence.removed_policy_keys
                    or record.evidence.changed_policy_keys
                ),
                metrics=merge_train_policy_metrics,
            ),
            blast_radius=PrivilegedOperationSemanticReviewBlastRadius(
                scope="merge_train_policy",
                summary="Bounded to merge-train policy target counts; target identities are redacted.",
                affected_count=record.evidence.candidate_target_count,
            ),
            rollback=PrivilegedOperationSemanticReviewRollback(
                rollback_class="policy_cas",
                summary="Rollback is bounded by merge-train policy CAS and record digest evidence.",
            ),
            evidence=PrivilegedOperationSemanticReviewEvidence(
                result_status=_semantic_review_result_status(record),
                digests=_semantic_review_digests(
                    record,
                    (
                        PrivilegedOperationSemanticReviewDigest(
                            kind="active_merge_train_policy",
                            label="Active merge-train policy digest",
                            sha256=record.evidence.active_policy_sha256,
                        ),
                        PrivilegedOperationSemanticReviewDigest(
                            kind="candidate_merge_train_policy",
                            label="Candidate merge-train policy digest",
                            sha256=record.evidence.candidate_policy_sha256,
                        ),
                    ),
                ),
            ),
            activity=_semantic_review_activity(record.operation_id, events),
            can_approve=record.status == "planned" and not merge_train_blocker_codes,
            can_revoke=record.status == "approved" and lifecycle.expiry_state == "active",
        )
    raise PrivilegedOperationSemanticReviewError(
        "Privileged-operation semantic review descriptor is unsupported."
    )


validate_privileged_operation_semantic_review_coverage()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _read_privileged_operation_event(
    *,
    store: PrivilegedOperationStore,
    operation_id: str,
    event_id: str,
) -> PrivilegedOperationEventRecord | None:
    return next(
        (
            event
            for event in store.list_privileged_operation_event_records(
                operation_id=operation_id,
                limit=10,
            )
            if event.event_id == event_id
        ),
        None,
    )


def _replay_existing_plan(
    *,
    store: PrivilegedOperationStore,
    operation_id: str,
    actor: PrivilegedOperationRequester,
    descriptor_id: PrivilegedOperationDescriptorId,
    source_kind: PrivilegedOperationSourceKind,
    source_event_id: str,
    request: PrivilegedOperationRequest,
    expires_in_seconds: int,
) -> PrivilegedOperationWriteResult | None:
    try:
        record = store.read_privileged_operation_record(operation_id)
    except FileNotFoundError:
        return None
    expected_expiry = datetime.fromisoformat(record.created_at) + timedelta(
        seconds=expires_in_seconds
    )
    registration = read_privileged_operation_descriptor(descriptor_id)
    if (
        record.descriptor_id != registration.descriptor.descriptor_id
        or record.descriptor_version != registration.descriptor.descriptor_version
        or record.safety_class != registration.descriptor.safety_class
        or record.source_event_id != source_event_id
        or record.requested_by != actor
        or record.request_digest not in privileged_operation_request_digest_candidates(request)
        or datetime.fromisoformat(record.expires_at) != expected_expiry
    ):
        raise PrivilegedOperationConflictError(
            "Privileged-operation plan replay changed the original request."
        )
    event_id = build_privileged_operation_event_id(
        operation_id=operation_id,
        action="planned",
        source_kind=source_kind,
        source_event_id=source_event_id,
    )
    event = _read_privileged_operation_event(
        store=store,
        operation_id=operation_id,
        event_id=event_id,
    )
    if event is None:
        raise PrivilegedOperationConflictError(
            "Privileged-operation plan exists without its planned event."
        )
    return PrivilegedOperationWriteResult(
        write_status="replayed",
        record=record,
        event=event,
    )


def create_typed_privileged_operation_plan(
    *,
    record_store: object,
    descriptor_id: PrivilegedOperationDescriptorId,
    actor: PrivilegedOperationRequester,
    source_kind: PrivilegedOperationSourceKind,
    source_event_id: str,
    request: PrivilegedOperationRequest,
    expires_in_seconds: int = DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS,
    now: Callable[[], datetime] = _utc_now,
) -> PrivilegedOperationWriteResult:
    if (
        not MIN_PRIVILEGED_OPERATION_TTL_SECONDS
        <= expires_in_seconds
        <= MAX_PRIVILEGED_OPERATION_TTL_SECONDS
    ):
        raise ValueError("Privileged-operation plan expiry must be between 300 and 86400 seconds.")
    expected_source_kind = "agent_api" if actor.identity_type == "terminal_agent" else "browser_api"
    if source_kind != expected_source_kind:
        raise ValueError("Privileged-operation plan source does not match requester identity.")
    store = require_privileged_operation_store(record_store)
    operation_id = build_privileged_operation_id_for_actor(
        descriptor_id=descriptor_id,
        actor=actor,
        source_event_id=source_event_id,
    )
    replay = _replay_existing_plan(
        store=store,
        operation_id=operation_id,
        actor=actor,
        descriptor_id=descriptor_id,
        source_kind=source_kind,
        source_event_id=source_event_id,
        request=request,
        expires_in_seconds=expires_in_seconds,
    )
    if replay is not None:
        return replay
    registration = read_privileged_operation_descriptor(descriptor_id)
    evidence = registration.planner(record_store, request)
    created_at = now().astimezone(timezone.utc)
    record = PrivilegedOperationRecord(
        operation_id=operation_id,
        descriptor_id=registration.descriptor.descriptor_id,
        descriptor_version=registration.descriptor.descriptor_version,
        safety_class=registration.descriptor.safety_class,
        status="planned",
        source_event_id=source_event_id,
        requested_by=actor,
        request=request,
        request_digest=privileged_operation_request_digest(request),
        evidence=evidence,
        evidence_digest=privileged_operation_evidence_digest(evidence),
        created_at=_timestamp(created_at),
        updated_at=_timestamp(created_at),
        expires_at=_timestamp(created_at + timedelta(seconds=expires_in_seconds)),
    )
    event = PrivilegedOperationEventRecord(
        operation_id=operation_id,
        sequence=1,
        action="planned",
        occurred_at=record.created_at,
        source_kind=source_kind,
        source_event_id=source_event_id,
        actor=actor,
        resulting_record_digest=privileged_operation_record_digest(record),
    )
    try:
        write_status = store.write_privileged_operation_plan(record, event)
    except PrivilegedOperationConflictError:
        replay = _replay_existing_plan(
            store=store,
            operation_id=operation_id,
            actor=actor,
            descriptor_id=descriptor_id,
            source_kind=source_kind,
            source_event_id=source_event_id,
            request=request,
            expires_in_seconds=expires_in_seconds,
        )
        if replay is not None:
            return replay
        raise
    persisted_record = store.read_privileged_operation_record(operation_id)
    persisted_event = next(
        (
            stored_event
            for stored_event in store.list_privileged_operation_event_records(
                operation_id=operation_id,
                limit=2,
            )
            if stored_event.event_id == event.event_id
        ),
        event,
    )
    return PrivilegedOperationWriteResult(
        write_status=write_status,
        record=persisted_record,
        event=persisted_event,
    )


def create_privileged_operation_plan(
    *,
    record_store: object,
    requester_github_id: int,
    requester_login: str,
    source_event_id: str,
    request: ManagedSecretReencryptionPlanInput,
    expires_in_seconds: int = DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS,
    now: Callable[[], datetime] = _utc_now,
) -> PrivilegedOperationWriteResult:
    """Preserve the original managed-secret planning API and deterministic IDs."""

    actor = PrivilegedOperationActor(
        identity_type="github_human",
        github_id=requester_github_id,
        login=requester_login,
    )
    expected_operation_id = build_privileged_operation_id(
        github_id=requester_github_id,
        source_event_id=source_event_id,
    )
    result = create_typed_privileged_operation_plan(
        record_store=record_store,
        descriptor_id="managed-secret-reencryption",
        actor=actor,
        source_kind="browser_api",
        source_event_id=source_event_id,
        request=request,
        expires_in_seconds=expires_in_seconds,
        now=now,
    )
    if result.record.operation_id != expected_operation_id:
        raise PrivilegedOperationConflictError(
            "Managed-secret privileged-operation identity compatibility was not preserved."
        )
    return result


def expire_privileged_operation_if_due(
    *,
    record_store: object,
    record: PrivilegedOperationRecord,
    now: Callable[[], datetime] = _utc_now,
) -> PrivilegedOperationRecord:
    if record.status not in {"planned", "approved"}:
        return record
    current_time = now().astimezone(timezone.utc)
    if current_time < datetime.fromisoformat(record.expires_at):
        return record
    store = require_privileged_operation_store(record_store)
    occurred_at = _timestamp(current_time)
    reason = (
        "Privileged-operation plan expired before approval."
        if record.status == "planned"
        else "Privileged-operation approval expired before execution began."
    )
    sequence = 2 if record.status == "planned" else 3
    expired_record = record.model_copy(
        update={
            "status": "expired",
            "updated_at": occurred_at,
            "terminal_at": occurred_at,
            "terminal_reason": reason,
        }
    )
    source_event_id = f"expiry:{record.operation_id}"
    event = PrivilegedOperationEventRecord(
        event_id=build_privileged_operation_event_id(
            operation_id=record.operation_id,
            action="expired",
            source_kind="system",
            source_event_id=source_event_id,
        ),
        operation_id=record.operation_id,
        sequence=sequence,
        action="expired",
        occurred_at=occurred_at,
        source_kind="system",
        source_event_id=source_event_id,
        actor=PrivilegedOperationActor(identity_type="system", login="system"),
        reason=reason,
        resulting_record_digest=privileged_operation_record_digest(expired_record),
    )
    try:
        store.transition_privileged_operation(expired_record, event)
    except PrivilegedOperationConflictError:
        return store.read_privileged_operation_record(record.operation_id)
    return expired_record


def read_privileged_operation(
    *,
    record_store: object,
    operation_id: str,
    now: Callable[[], datetime] = _utc_now,
) -> PrivilegedOperationRecord:
    store = require_privileged_operation_store(record_store)
    record = store.read_privileged_operation_record(operation_id)
    return expire_privileged_operation_if_due(
        record_store=store,
        record=record,
        now=now,
    )


def list_privileged_operations(
    *,
    record_store: object,
    status: str = "",
    descriptor_id: str = "",
    limit: int | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> tuple[PrivilegedOperationRecord, ...]:
    store = require_privileged_operation_store(record_store)
    if status not in {"cancelled", "revoked", "executed", "execution_failed"}:
        expiry_statuses = (
            (status,) if status in {"planned", "approved"} else ("planned", "approved")
        )
        for expiry_status in expiry_statuses:
            for record in store.list_privileged_operation_records(
                status=expiry_status,
                descriptor_id=descriptor_id,
            ):
                expire_privileged_operation_if_due(record_store=store, record=record, now=now)
    return store.list_privileged_operation_records(
        status=status,
        descriptor_id=descriptor_id,
        limit=limit,
    )


def cancel_privileged_operation(
    *,
    record_store: object,
    operation_id: str,
    actor_github_id: int,
    actor_login: str,
    source_event_id: str,
    reason: str,
    now: Callable[[], datetime] = _utc_now,
) -> PrivilegedOperationWriteResult:
    store = require_privileged_operation_store(record_store)
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("Privileged-operation cancellation requires a reason.")
    actor = PrivilegedOperationActor(
        identity_type="github_human",
        github_id=actor_github_id,
        login=actor_login,
    )
    event_id = build_privileged_operation_event_id(
        operation_id=operation_id,
        action="cancelled",
        source_kind="browser_api",
        source_event_id=source_event_id,
    )
    record = read_privileged_operation(
        record_store=store,
        operation_id=operation_id,
        now=now,
    )
    existing_event = _read_privileged_operation_event(
        store=store,
        operation_id=operation_id,
        event_id=event_id,
    )
    if existing_event is not None:
        if (
            record.status == "cancelled"
            and existing_event.action == "cancelled"
            and existing_event.actor == actor
            and existing_event.reason == normalized_reason
            and existing_event.resulting_record_digest == privileged_operation_record_digest(record)
        ):
            return PrivilegedOperationWriteResult(
                write_status="replayed",
                record=record,
                event=existing_event,
            )
        raise PrivilegedOperationConflictError(
            "Privileged-operation cancellation replay changed the original request."
        )
    if record.status != "planned":
        raise PrivilegedOperationNotCancellableError(
            f"Privileged-operation plan is already {record.status}."
        )
    occurred_at = _timestamp(now().astimezone(timezone.utc))
    cancelled_record = record.model_copy(
        update={
            "status": "cancelled",
            "updated_at": occurred_at,
            "terminal_at": occurred_at,
            "terminal_reason": normalized_reason,
        }
    )
    event = PrivilegedOperationEventRecord(
        event_id=event_id,
        operation_id=operation_id,
        sequence=2,
        action="cancelled",
        occurred_at=occurred_at,
        source_kind="browser_api",
        source_event_id=source_event_id,
        actor=actor,
        reason=normalized_reason,
        resulting_record_digest=privileged_operation_record_digest(cancelled_record),
    )
    try:
        write_status = store.transition_privileged_operation(cancelled_record, event)
    except PrivilegedOperationConflictError:
        persisted_record = store.read_privileged_operation_record(operation_id)
        persisted_event = _read_privileged_operation_event(
            store=store,
            operation_id=operation_id,
            event_id=event_id,
        )
        if (
            persisted_event is not None
            and persisted_record.status == "cancelled"
            and persisted_event.actor == actor
            and persisted_event.reason == normalized_reason
            and persisted_event.resulting_record_digest
            == privileged_operation_record_digest(persisted_record)
        ):
            return PrivilegedOperationWriteResult(
                write_status="replayed",
                record=persisted_record,
                event=persisted_event,
            )
        raise
    return PrivilegedOperationWriteResult(
        write_status=write_status,
        record=store.read_privileged_operation_record(operation_id),
        event=event,
    )


def approve_privileged_operation(
    *,
    record_store: object,
    operation_id: str,
    approval: PrivilegedOperationApproval,
    source_event_id: str,
    now: Callable[[], datetime] = _utc_now,
) -> PrivilegedOperationWriteResult:
    store = require_privileged_operation_store(record_store)
    record = read_privileged_operation(record_store=store, operation_id=operation_id, now=now)
    event_id = build_privileged_operation_event_id(
        operation_id=operation_id,
        action="approved",
        source_kind="browser_api",
        source_event_id=source_event_id,
    )
    existing_event = _read_privileged_operation_event(
        store=store,
        operation_id=operation_id,
        event_id=event_id,
    )
    if existing_event is not None:
        if record.status == "approved" and record.approval == approval:
            return PrivilegedOperationWriteResult("replayed", record, existing_event)
        raise PrivilegedOperationConflictError(
            "Privileged-operation approval replay changed the original request."
        )
    if record.status != "planned":
        raise PrivilegedOperationNotApprovableError(
            f"Privileged-operation plan is already {record.status}."
        )
    if (
        isinstance(record.evidence, ManagedAuthzPolicySetHumanEvidence)
        and record.evidence.result_status == "blocked"
    ):
        raise PrivilegedOperationNotApprovableError(
            "Managed authz policy operations with blockers cannot be approved."
        )
    occurred_at = _timestamp(now().astimezone(timezone.utc))
    approved = record.model_copy(
        update={"status": "approved", "approval": approval, "updated_at": occurred_at}
    )
    event = PrivilegedOperationEventRecord(
        event_id=event_id,
        operation_id=operation_id,
        sequence=2,
        action="approved",
        occurred_at=occurred_at,
        source_kind="browser_api",
        source_event_id=source_event_id,
        actor=approval.approver,
        reason=approval.reason,
        resulting_record_digest=privileged_operation_record_digest(approved),
    )
    write_status = store.transition_privileged_operation(approved, event)
    return PrivilegedOperationWriteResult(
        write_status, store.read_privileged_operation_record(operation_id), event
    )


def revoke_privileged_operation(
    *,
    record_store: object,
    operation_id: str,
    actor_github_id: int,
    actor_login: str,
    source_event_id: str,
    reason: str,
    now: Callable[[], datetime] = _utc_now,
) -> PrivilegedOperationWriteResult:
    store = require_privileged_operation_store(record_store)
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("Privileged-operation revocation requires a reason.")
    actor = PrivilegedOperationActor(
        identity_type="github_human", github_id=actor_github_id, login=actor_login
    )
    record = read_privileged_operation(record_store=store, operation_id=operation_id, now=now)
    event_id = build_privileged_operation_event_id(
        operation_id=operation_id,
        action="revoked",
        source_kind="browser_api",
        source_event_id=source_event_id,
    )
    existing_event = _read_privileged_operation_event(
        store=store,
        operation_id=operation_id,
        event_id=event_id,
    )
    if existing_event is not None:
        if (
            record.status == "revoked"
            and existing_event.actor == actor
            and existing_event.reason == normalized_reason
        ):
            return PrivilegedOperationWriteResult("replayed", record, existing_event)
        raise PrivilegedOperationConflictError(
            "Privileged-operation revocation replay changed the original request."
        )
    if record.status != "approved":
        raise PrivilegedOperationNotRevocableError(
            f"Privileged-operation plan is already {record.status}."
        )
    occurred_at = _timestamp(now().astimezone(timezone.utc))
    revoked = record.model_copy(
        update={
            "status": "revoked",
            "updated_at": occurred_at,
            "terminal_at": occurred_at,
            "terminal_reason": normalized_reason,
        }
    )
    event = PrivilegedOperationEventRecord(
        event_id=event_id,
        operation_id=operation_id,
        sequence=3,
        action="revoked",
        occurred_at=occurred_at,
        source_kind="browser_api",
        source_event_id=source_event_id,
        actor=actor,
        reason=normalized_reason,
        resulting_record_digest=privileged_operation_record_digest(revoked),
    )
    write_status = store.transition_privileged_operation(revoked, event)
    return PrivilegedOperationWriteResult(
        write_status, store.read_privileged_operation_record(operation_id), event
    )
