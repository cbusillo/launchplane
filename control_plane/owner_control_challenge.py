from __future__ import annotations

from datetime import datetime, timezone

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.owner_control import ApprovalRequest, ReviewItem, ServerReviewPayload
from control_plane.contracts.privileged_operation import (
    ManagedAuthzPolicySetHumanEvidence,
    ManagedAuthzPolicySetProposalInput,
    ManagedSecretReencryptionHumanEvidence,
    PrivilegedOperationRecord,
    privileged_operation_pre_state_digest,
)
from control_plane.service_auth import AuthorizationTarget


class OwnerControlChallengeProvenanceError(ValueError):
    """Raised when a planned operation cannot produce a safe owner-control challenge."""


def _canonical_timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise OwnerControlChallengeProvenanceError(
            f"{field_name} must be an ISO-8601 timestamp."
        ) from error
    if parsed.tzinfo is None:
        raise OwnerControlChallengeProvenanceError(f"{field_name} must include a timezone.")
    canonical = parsed.astimezone(timezone.utc).isoformat()
    if canonical != value or parsed.microsecond:
        raise OwnerControlChallengeProvenanceError(
            f"{field_name} must use whole-second canonical UTC form."
        )
    return parsed


def _review_payload(
    *,
    operation: PrivilegedOperationRecord,
    policy_record: LaunchplaneAuthzPolicyRecord,
) -> ServerReviewPayload:
    from control_plane.privileged_operation_registry import read_privileged_operation_descriptor

    descriptor = read_privileged_operation_descriptor(operation.descriptor_id).descriptor
    if (
        operation.descriptor_version != descriptor.descriptor_version
        or operation.safety_class != descriptor.safety_class
    ):
        raise OwnerControlChallengeProvenanceError(
            "Planned operation descriptor provenance changed."
        )

    items: tuple[ReviewItem, ...]
    base_items = (
        ReviewItem(key="operation_id", label="Operation", value=operation.operation_id),
        ReviewItem(key="operation_class", label="Operation class", value=operation.descriptor_id),
        ReviewItem(key="safety_class", label="Safety class", value=operation.safety_class),
        ReviewItem(key="request_digest", label="Request digest", value=operation.request_digest),
        ReviewItem(key="plan_digest", label="Plan digest", value=operation.evidence.plan_digest),
        ReviewItem(key="evidence_digest", label="Evidence digest", value=operation.evidence_digest),
        ReviewItem(
            key="pre_state_digest",
            label="Planned pre-state digest",
            value=privileged_operation_pre_state_digest(operation.evidence),
        ),
        ReviewItem(
            key="policy_revision", label="Policy revision", value=str(policy_record.revision)
        ),
        ReviewItem(key="policy_digest", label="Policy digest", value=policy_record.policy_sha256),
        ReviewItem(
            key="operation_expires_at", label="Operation expires", value=operation.expires_at
        ),
        ReviewItem(
            key="planning_result",
            label="Planning result",
            value=operation.evidence.result_status,
        ),
    )
    if isinstance(operation.evidence, ManagedSecretReencryptionHumanEvidence):
        items = base_items + (
            ReviewItem(
                key="configured_secret_count",
                label="Configured secrets",
                value=str(operation.evidence.configured_secret_count),
            ),
            ReviewItem(
                key="rotation_candidate_count",
                label="Rotation candidates",
                value=str(operation.evidence.rotation_candidate_count),
            ),
            ReviewItem(
                key="unchanged_count",
                label="Unchanged secrets",
                value=str(operation.evidence.unchanged_count),
            ),
            ReviewItem(
                key="unreadable_secret_count",
                label="Unreadable secrets",
                value=str(operation.evidence.unreadable_secret_count),
            ),
        )
    elif isinstance(operation.evidence, ManagedAuthzPolicySetHumanEvidence):
        if not isinstance(operation.request, ManagedAuthzPolicySetProposalInput):
            raise OwnerControlChallengeProvenanceError("Unsupported managed-policy request.")
        if operation.evidence.result_status == "blocked":
            raise OwnerControlChallengeProvenanceError(
                "Blocked managed-policy plans cannot be challenged."
            )
        items = base_items + (
            ReviewItem(
                key="changed",
                label="Policy changes",
                value="yes" if operation.evidence.diff.changed else "no",
            ),
            ReviewItem(
                key="added_rule_count",
                label="Added rules",
                value=str(operation.evidence.diff.added_rule_count),
            ),
            ReviewItem(
                key="adopted_rule_count",
                label="Adopted rules",
                value=str(operation.evidence.diff.adopted_rule_count),
            ),
            ReviewItem(
                key="updated_rule_count",
                label="Updated rules",
                value=str(operation.evidence.diff.updated_rule_count),
            ),
            ReviewItem(
                key="removed_rule_count",
                label="Removed rules",
                value=str(operation.evidence.diff.removed_rule_count),
            ),
            ReviewItem(
                key="unchanged_rule_count",
                label="Unchanged rules",
                value=str(operation.evidence.diff.unchanged_rule_count),
            ),
            ReviewItem(
                key="managed_set_digest",
                label="Managed set digest",
                value=canonical_json_sha256({"managed_set_id": operation.request.managed_set_id}),
            ),
            ReviewItem(
                key="policy_safety_blocker_count",
                label="Policy safety blockers",
                value=str(operation.evidence.diff.policy_safety_blocker_count),
            ),
            ReviewItem(
                key="operational_readiness_blocked_rule_count",
                label="Operational readiness blockers",
                value=str(operation.evidence.diff.operational_readiness_blocked_rule_count),
            ),
        )
    else:
        raise OwnerControlChallengeProvenanceError("Unsupported planned operation evidence.")
    review_digest = canonical_json_sha256(
        {
            "items": [item.model_dump(mode="json") for item in items],
            "operation_id": operation.operation_id,
            "policy_record_id": policy_record.record_id,
        }
    )
    return ServerReviewPayload(
        review_id=f"owner-control-review-{review_digest[:24]}",
        title="Owner approval required",
        summary="Review this exact planned privileged operation before confirming.",
        items=items,
    )


def derive_owner_control_approval_request(
    *,
    operation: PrivilegedOperationRecord,
    policy_record: LaunchplaneAuthzPolicyRecord,
    owner_github_id: int,
    nonce: str,
    issued_at: str,
    expires_at: str,
) -> ApprovalRequest:
    """Construct a fully server-authored challenge from locked current provenance."""

    if operation.status != "planned":
        raise OwnerControlChallengeProvenanceError("Only planned operations can be challenged.")
    if policy_record.status != "active":
        raise OwnerControlChallengeProvenanceError(
            "Owner-control challenges require an active policy."
        )
    if policy_record.policy.schema_version != 2:
        raise OwnerControlChallengeProvenanceError(
            "Owner-control challenges require schema-v2 authz policy."
        )
    _canonical_timestamp(issued_at, "issued_at")
    if _canonical_timestamp(expires_at, "expires_at") <= _canonical_timestamp(
        issued_at, "issued_at"
    ):
        raise OwnerControlChallengeProvenanceError("Challenge expiry must follow issuance.")
    from control_plane.privileged_operation_registry import read_privileged_operation_descriptor

    descriptor = read_privileged_operation_descriptor(operation.descriptor_id).descriptor
    matching_rules = tuple(
        rule
        for rule in policy_record.policy.github_humans
        if rule.managed_set_id
        and rule.managed_rule_id
        and owner_github_id in rule.github_ids
        and not rule.logins
        and not rule.organizations
        and not rule.teams
        and not rule.roles
        and rule.allows_scope(
            action=descriptor.approve_action,
            product="launchplane",
            context="launchplane",
            target=AuthorizationTarget(scope="global"),
            schema_version=policy_record.policy.schema_version,
        )
    )
    if len(matching_rules) != 1:
        raise OwnerControlChallengeProvenanceError(
            "Enrolled owner requires exactly one immutable GitHub-ID-only approval rule."
        )
    return ApprovalRequest(
        operation_id=operation.operation_id,
        descriptor_id=operation.descriptor_id,
        descriptor_version=1,
        request_digest=operation.request_digest,
        plan_digest=operation.evidence.plan_digest,
        evidence_digest=operation.evidence_digest,
        pre_state_digest=privileged_operation_pre_state_digest(operation.evidence),
        policy_record_id=policy_record.record_id,
        policy_revision=policy_record.revision,
        policy_sha256=policy_record.policy_sha256,
        owner_github_id=owner_github_id,
        server_review=_review_payload(operation=operation, policy_record=policy_record),
        nonce=nonce,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def owner_control_challenge_semantics(request: ApprovalRequest) -> dict[str, object]:
    """Return the immutable provenance that makes repeated issuance idempotent."""

    payload = request.model_dump(mode="json")
    payload.pop("nonce")
    payload.pop("issued_at")
    payload.pop("expires_at")
    return payload
