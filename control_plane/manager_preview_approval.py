from __future__ import annotations

from typing import NamedTuple, Protocol, cast

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.manager_preview_approval import (
    MANAGER_PREVIEW_APPROVAL_WRITE_ACTION,
    ManagerPreviewApprovalAction,
    ManagerPreviewApprovalAuthorization,
    ManagerPreviewApprovalBinding,
    ManagerPreviewApprovalDecision,
    ManagerPreviewApprovalDecisionStatus,
    ManagerPreviewApprovalEventRecord,
    ManagerPreviewApprovalEventWriteStatus,
    ManagerPreviewApprovalReasonCode,
    immutable_image_digest,
)
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.service_auth import AuthorizationTarget, GitHubHumanIdentity


class ManagerPreviewApprovalEvidenceError(ValueError):
    def __init__(self, *, code: ManagerPreviewApprovalReasonCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class ManagerPreviewApprovalAuthorizationError(PermissionError):
    pass


class ManagerPreviewApprovalEventConflictError(RuntimeError):
    pass


class ManagerPreviewApprovalEventStore(Protocol):
    def write_manager_preview_approval_event_record(
        self, record: ManagerPreviewApprovalEventRecord
    ) -> ManagerPreviewApprovalEventWriteStatus:
        raise NotImplementedError

    def list_manager_preview_approval_event_records(
        self,
        *,
        product: str = "",
        context: str = "",
        repository: str = "",
        pr_number: int | None = None,
        preview_id: str = "",
        action: str = "",
        limit: int | None = None,
    ) -> tuple[ManagerPreviewApprovalEventRecord, ...]:
        raise NotImplementedError


class ManagerPreviewApprovalWriteResult(NamedTuple):
    status: ManagerPreviewApprovalEventWriteStatus
    record: ManagerPreviewApprovalEventRecord


def build_current_manager_preview_approval_binding(
    *,
    product: str,
    preview: PreviewRecord,
    generation: PreviewGenerationRecord,
) -> ManagerPreviewApprovalBinding:
    if preview.state != "active":
        raise ManagerPreviewApprovalEvidenceError(
            code="preview_inactive",
            message="The preview is not active.",
        )
    if not preview.serving_generation_id:
        raise ManagerPreviewApprovalEvidenceError(
            code="serving_generation_missing",
            message="The preview does not have a serving generation.",
        )
    if (
        generation.preview_id != preview.preview_id
        or generation.generation_id != preview.serving_generation_id
    ):
        raise ManagerPreviewApprovalEvidenceError(
            code="serving_generation_mismatch",
            message="The supplied generation is not the preview's serving generation.",
        )
    if generation.state != "ready":
        raise ManagerPreviewApprovalEvidenceError(
            code="generation_not_ready",
            message="The serving preview generation is not ready.",
        )
    if any(
        status != "pass"
        for status in (
            generation.deploy_status,
            generation.verify_status,
            generation.overall_health_status,
        )
    ):
        raise ManagerPreviewApprovalEvidenceError(
            code="generation_verification_failed",
            message="The serving preview generation has not passed deployment and verification.",
        )
    if (
        generation.anchor_summary.repo.lower() != preview.anchor_repo.lower()
        or generation.anchor_summary.pr_number != preview.anchor_pr_number
        or generation.anchor_summary.pr_url != preview.anchor_pr_url
        or preview.latest_manifest_fingerprint != generation.resolved_manifest_fingerprint
    ):
        raise ManagerPreviewApprovalEvidenceError(
            code="preview_identity_mismatch",
            message="The preview and serving generation identity do not match.",
        )
    if not generation.artifact_id:
        raise ManagerPreviewApprovalEvidenceError(
            code="artifact_identity_missing",
            message="The serving preview generation does not have immutable artifact identity.",
        )
    if generation.runtime_identity is None:
        raise ManagerPreviewApprovalEvidenceError(
            code="runtime_identity_missing",
            message="The serving preview generation does not have verified runtime identity.",
        )
    try:
        artifact_image_digest = immutable_image_digest(generation.runtime_identity.image_reference)
        return ManagerPreviewApprovalBinding(
            product=product,
            context=preview.context,
            repository=preview.anchor_repo,
            pr_number=preview.anchor_pr_number,
            pr_url=preview.anchor_pr_url,
            head_sha=generation.anchor_summary.head_sha,
            preview_id=preview.preview_id,
            serving_generation_id=generation.generation_id,
            artifact_id=generation.artifact_id,
            artifact_image_digest=artifact_image_digest,
            manifest_fingerprint=generation.resolved_manifest_fingerprint,
            preview_url=preview.canonical_url,
            runtime_identity=generation.runtime_identity,
        )
    except ValueError as error:
        raise ManagerPreviewApprovalEvidenceError(
            code="runtime_identity_mismatch",
            message="The serving preview runtime identity is incomplete or inconsistent.",
        ) from error


def capture_manager_preview_approval_authorization(
    *,
    identity: GitHubHumanIdentity,
    product: str,
    context: str,
    policy_record: LaunchplaneAuthzPolicyRecord,
    authorized_at: str,
) -> ManagerPreviewApprovalAuthorization:
    if identity.github_id < 1:
        raise ManagerPreviewApprovalAuthorizationError(
            "Manager preview approval requires a stable GitHub numeric identity."
        )
    if policy_record.status != "active" or policy_record.policy.schema_version != 2:
        raise ManagerPreviewApprovalAuthorizationError(
            "Manager preview approval requires one active schema-v2 authorization policy."
        )
    target = AuthorizationTarget(scope="context")
    matching_rules = tuple(
        rule
        for rule in policy_record.policy.github_humans
        if rule.managed_set_id is not None
        and rule.managed_rule_id is not None
        and identity.github_id in rule.github_ids
        and MANAGER_PREVIEW_APPROVAL_WRITE_ACTION in rule.actions
        and rule.allows(
            identity=identity,
            action=MANAGER_PREVIEW_APPROVAL_WRITE_ACTION,
            product=product,
            context=context,
            target=target,
            schema_version=2,
        )
    )
    if len(matching_rules) != 1:
        raise ManagerPreviewApprovalAuthorizationError(
            "Manager preview approval requires exactly one managed policy rule for the actor."
        )
    matching_rule = matching_rules[0]
    return ManagerPreviewApprovalAuthorization(
        manager_github_id=identity.github_id,
        manager_login=identity.login,
        managed_set_id=cast(str, matching_rule.managed_set_id),
        managed_rule_id=cast(str, matching_rule.managed_rule_id),
        policy_record_id=policy_record.record_id,
        policy_revision=policy_record.revision,
        policy_sha256=policy_record.policy_sha256,
        policy_source=policy_record.source,
        authorized_at=authorized_at,
    )


def record_manager_preview_approval_event(
    *,
    record_store: ManagerPreviewApprovalEventStore,
    identity: GitHubHumanIdentity,
    policy_record: LaunchplaneAuthzPolicyRecord,
    product: str,
    preview: PreviewRecord,
    generation: PreviewGenerationRecord,
    action: ManagerPreviewApprovalAction,
    occurred_at: str,
    source_event_kind: str,
    source_event_id: str,
    reason: str = "",
) -> ManagerPreviewApprovalWriteResult:
    if action not in {"approved", "changes_requested", "revoked"}:
        raise ValueError("Manager-authored preview approval events require a manager action.")
    binding = build_current_manager_preview_approval_binding(
        product=product,
        preview=preview,
        generation=generation,
    )
    authorization = capture_manager_preview_approval_authorization(
        identity=identity,
        product=binding.product,
        context=binding.context,
        policy_record=policy_record,
        authorized_at=occurred_at,
    )
    record = ManagerPreviewApprovalEventRecord(
        binding=binding,
        action=action,
        occurred_at=occurred_at,
        source_event_kind=source_event_kind,
        source_event_id=source_event_id,
        reason=reason,
        authorization=authorization,
    )
    status = record_store.write_manager_preview_approval_event_record(record)
    return ManagerPreviewApprovalWriteResult(status=status, record=record)


def build_manager_preview_approval_system_event(
    *,
    binding: ManagerPreviewApprovalBinding,
    action: ManagerPreviewApprovalAction,
    occurred_at: str,
    source_event_kind: str,
    source_event_id: str,
    reason: str,
) -> ManagerPreviewApprovalEventRecord:
    if action not in {"superseded", "invalidated"}:
        raise ValueError("System preview approval events require superseded or invalidated action.")
    return ManagerPreviewApprovalEventRecord(
        binding=binding,
        action=action,
        occurred_at=occurred_at,
        source_event_kind=source_event_kind,
        source_event_id=source_event_id,
        reason=reason,
    )


def evaluate_manager_preview_approval(
    *,
    product: str,
    preview: PreviewRecord,
    generation: PreviewGenerationRecord,
    policy_record: LaunchplaneAuthzPolicyRecord | None,
    events: tuple[ManagerPreviewApprovalEventRecord, ...],
    evaluated_at: str,
) -> ManagerPreviewApprovalDecision:
    """Evaluate current evidence against the complete approval history for the PR."""
    try:
        binding = build_current_manager_preview_approval_binding(
            product=product,
            preview=preview,
            generation=generation,
        )
    except ManagerPreviewApprovalEvidenceError as error:
        return _decision(
            status="unavailable",
            reason_code=error.code,
            reason=str(error),
            evaluated_at=evaluated_at,
        )
    if (
        policy_record is None
        or policy_record.status != "active"
        or policy_record.policy.schema_version != 2
    ):
        return _decision(
            status="unavailable",
            reason_code="policy_unavailable",
            reason="The active manager authorization policy is unavailable.",
            binding=binding,
            evaluated_at=evaluated_at,
        )

    subject_events = tuple(
        event
        for event in events
        if event.binding.product == binding.product
        and event.binding.context == binding.context
        and event.binding.repository == binding.repository
        and event.binding.pr_number == binding.pr_number
    )
    exact_events = tuple(
        event for event in subject_events if event.binding.binding_sha256 == binding.binding_sha256
    )
    if not exact_events:
        if subject_events:
            return _decision(
                status="stale",
                reason_code="approval_stale",
                reason="Prior approval evidence does not match the current preview.",
                binding=binding,
                evaluated_at=evaluated_at,
            )
        return _decision(
            status="pending",
            reason_code="approval_missing",
            reason="The current preview has not been approved by the manager.",
            binding=binding,
            evaluated_at=evaluated_at,
        )

    latest_event = max(exact_events, key=lambda event: (event.occurred_at, event.event_id))
    if latest_event.action == "approved":
        if not _approval_authorization_matches_policy(
            event=latest_event,
            policy_record=policy_record,
        ):
            return _decision(
                status="stale",
                reason_code="approval_stale",
                reason="The approval was recorded under a different authorization policy.",
                binding=binding,
                event=latest_event,
                evaluated_at=evaluated_at,
            )
        return _decision(
            status="approved",
            reason_code="approval_valid",
            reason="The manager approved the exact current preview.",
            binding=binding,
            event=latest_event,
            evaluated_at=evaluated_at,
        )
    if latest_event.action == "changes_requested":
        return _decision(
            status="changes_requested",
            reason_code="changes_requested",
            reason="The manager requested changes to the current preview.",
            binding=binding,
            event=latest_event,
            evaluated_at=evaluated_at,
        )
    if latest_event.action == "revoked":
        return _decision(
            status="revoked",
            reason_code="approval_revoked",
            reason="The manager revoked approval for the current preview.",
            binding=binding,
            event=latest_event,
            evaluated_at=evaluated_at,
        )
    return _decision(
        status="stale",
        reason_code="approval_stale",
        reason="The current preview approval was superseded or invalidated.",
        binding=binding,
        event=latest_event,
        evaluated_at=evaluated_at,
    )


def _approval_authorization_matches_policy(
    *,
    event: ManagerPreviewApprovalEventRecord,
    policy_record: LaunchplaneAuthzPolicyRecord,
) -> bool:
    authorization = event.authorization
    if authorization is None:
        return False
    if (
        authorization.policy_record_id != policy_record.record_id
        or authorization.policy_revision != policy_record.revision
        or authorization.policy_sha256 != policy_record.policy_sha256
    ):
        return False
    matching_rules = tuple(
        rule
        for rule in policy_record.policy.github_humans
        if rule.managed_set_id == authorization.managed_set_id
        and rule.managed_rule_id == authorization.managed_rule_id
    )
    if len(matching_rules) != 1:
        return False
    rule = matching_rules[0]
    return (
        authorization.manager_github_id in rule.github_ids
        and MANAGER_PREVIEW_APPROVAL_WRITE_ACTION in rule.actions
        and (not rule.products or event.binding.product in rule.products)
        and (not rule.contexts or event.binding.context in rule.contexts)
    )


def _decision(
    *,
    status: ManagerPreviewApprovalDecisionStatus,
    reason_code: ManagerPreviewApprovalReasonCode,
    reason: str,
    evaluated_at: str,
    binding: ManagerPreviewApprovalBinding | None = None,
    event: ManagerPreviewApprovalEventRecord | None = None,
) -> ManagerPreviewApprovalDecision:
    return ManagerPreviewApprovalDecision(
        status=status,
        reason_code=reason_code,
        reason=reason,
        current_binding_sha256=binding.binding_sha256 if binding is not None else "",
        event_id=event.event_id if event is not None else "",
        approval_id=event.approval_id if event is not None else "",
        manager_github_id=event.manager_github_id if event is not None else 0,
        manager_login=event.manager_login if event is not None else "",
        evaluated_at=evaluated_at,
    )
