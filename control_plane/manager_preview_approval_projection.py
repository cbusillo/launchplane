from __future__ import annotations

from collections.abc import Callable
from typing import Protocol, cast
from urllib.parse import quote

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.manager_preview_approval import (
    ManagerPreviewApprovalBinding,
    ManagerPreviewApprovalDecision,
    ManagerPreviewApprovalEventRecord,
)
from control_plane.contracts.manager_preview_approval_projection import (
    MANAGER_PREVIEW_APPROVAL_CHECK_NAME,
    MANAGER_PREVIEW_APPROVAL_COMMENT_MARKER,
    ManagerPreviewApprovalCheckState,
    ManagerPreviewApprovalProjection,
)
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.durable_operation_authorization import read_active_authz_policy_record
from control_plane.manager_preview_approval import (
    build_current_manager_preview_approval_binding,
    evaluate_manager_preview_approval,
    manager_preview_approval_required,
)
from control_plane.workflows.launchplane import github_api_request


GitHubApiRequest = Callable[..., object]


class ManagerPreviewApprovalProjectionStore(Protocol):
    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...

    def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord: ...

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
    ) -> tuple[ManagerPreviewApprovalEventRecord, ...]: ...

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]: ...


def build_manager_preview_approval_projection(
    *,
    record_store: ManagerPreviewApprovalProjectionStore,
    repository: str,
    pr_number: int,
    pr_url: str,
    pr_state: str,
    current_head_sha: str,
    evaluated_at: str,
) -> ManagerPreviewApprovalProjection:
    normalized_repository = repository.strip()
    normalized_pr_url = pr_url.strip()
    normalized_pr_state = pr_state.strip().lower() or "unknown"
    normalized_head_sha = current_head_sha.strip().lower()
    profiles = tuple(
        profile
        for profile in record_store.list_product_profile_records()
        if profile.repository.strip().casefold() == normalized_repository.casefold()
    )
    if len(profiles) != 1:
        return _unavailable_projection(
            required=False,
            product="unknown",
            context="unknown",
            repository=normalized_repository,
            pr_number=pr_number,
            pr_url=normalized_pr_url,
            pr_state=normalized_pr_state,
            head_sha=normalized_head_sha,
            reason="Launchplane could not resolve one product profile for this pull request.",
            evaluated_at=evaluated_at,
        )
    profile = profiles[0]
    context = profile.preview.context.strip()
    try:
        policy_record = read_active_authz_policy_record(record_store)
    except (LookupError, TypeError, ValueError):
        return _unavailable_projection(
            required=True,
            product=profile.product,
            context=context or "unknown",
            repository=normalized_repository,
            pr_number=pr_number,
            pr_url=normalized_pr_url,
            pr_state=normalized_pr_state,
            head_sha=normalized_head_sha,
            reason="The active manager authorization policy is unavailable.",
            evaluated_at=evaluated_at,
        )
    required = manager_preview_approval_required(
        policy_record=policy_record,
        product=profile.product,
        context=context,
    )
    if not required:
        return _unavailable_projection(
            required=False,
            product=profile.product,
            context=context or "unknown",
            repository=normalized_repository,
            pr_number=pr_number,
            pr_url=normalized_pr_url,
            pr_state=normalized_pr_state,
            head_sha=normalized_head_sha,
            reason="Manager preview approval is not required by the active product policy.",
            evaluated_at=evaluated_at,
            check_state="success",
        )

    anchor_repo = normalized_repository.split("/", 1)[1]
    previews = record_store.list_preview_records(
        context_name=context,
        anchor_repo=anchor_repo,
        anchor_pr_number=pr_number,
        limit=10,
    )
    preview = previews[0] if previews else None
    if preview is None or not preview.serving_generation_id.strip():
        return _unavailable_projection(
            required=True,
            product=profile.product,
            context=context,
            repository=normalized_repository,
            pr_number=pr_number,
            pr_url=normalized_pr_url,
            pr_state=normalized_pr_state,
            head_sha=normalized_head_sha,
            reason="The current pull request does not have a serving Launchplane preview.",
            evaluated_at=evaluated_at,
        )
    try:
        generation = record_store.read_preview_generation_record(preview.serving_generation_id)
    except (FileNotFoundError, LookupError, ValueError):
        return _unavailable_projection(
            required=True,
            product=profile.product,
            context=context,
            repository=normalized_repository,
            pr_number=pr_number,
            pr_url=normalized_pr_url,
            pr_state=normalized_pr_state,
            head_sha=normalized_head_sha,
            reason="The serving Launchplane preview generation is unavailable.",
            evaluated_at=evaluated_at,
            preview=preview,
        )
    events = record_store.list_manager_preview_approval_event_records(
        product=profile.product,
        context=context,
        repository=preview.anchor_repo,
        pr_number=pr_number,
        limit=200,
    )
    decision = evaluate_manager_preview_approval(
        product=profile.product,
        preview=preview,
        generation=generation,
        policy_record=policy_record,
        events=events,
        evaluated_at=evaluated_at,
    )
    binding: ManagerPreviewApprovalBinding | None = None
    if decision.current_binding_sha256:
        binding = build_current_manager_preview_approval_binding(
            product=profile.product,
            preview=preview,
            generation=generation,
        )
    if normalized_pr_state != "open":
        decision = _stale_decision(
            binding=binding,
            evaluated_at=evaluated_at,
            reason="The pull request is not open.",
        )
    elif binding is not None and normalized_head_sha != binding.head_sha:
        decision = _stale_decision(
            binding=binding,
            evaluated_at=evaluated_at,
            reason="The current pull request head does not match the serving preview.",
        )
    check_state = _check_state(decision)
    return ManagerPreviewApprovalProjection(
        required=True,
        product=profile.product,
        context=context,
        repository=normalized_repository,
        pr_number=pr_number,
        pr_url=normalized_pr_url,
        pr_state=normalized_pr_state,
        head_sha=normalized_head_sha,
        preview_id=preview.preview_id,
        preview_url=preview.canonical_url,
        serving_generation_id=generation.generation_id,
        artifact_id=generation.artifact_id,
        artifact_image_digest=binding.artifact_image_digest if binding is not None else "",
        manifest_fingerprint=generation.resolved_manifest_fingerprint,
        binding_sha256=binding.binding_sha256 if binding is not None else "",
        decision=decision,
        check_state=check_state,
        check_description=_check_description(decision),
        comment_markdown=_render_comment(
            preview=preview,
            generation=generation,
            binding=binding,
            decision=decision,
        ),
    )


def write_manager_preview_approval_projection(
    *,
    projection: ManagerPreviewApprovalProjection,
    token: str,
    api_request: GitHubApiRequest = github_api_request,
) -> dict[str, object]:
    owner, repo = projection.repository.split("/", 1)
    authenticated_user = api_request(path="/user", token=token)
    if not isinstance(authenticated_user, dict):
        raise ValueError("GitHub authenticated user response must be an object.")
    raw_authenticated_user_id = authenticated_user.get("id")
    authenticated_user_id = (
        raw_authenticated_user_id
        if isinstance(raw_authenticated_user_id, int)
        and not isinstance(raw_authenticated_user_id, bool)
        else 0
    )
    if authenticated_user_id < 1:
        raise ValueError("GitHub projection credential is missing stable actor identity.")
    comments_payload = api_request(
        path=(
            f"/repos/{quote(owner)}/{quote(repo)}/issues/{projection.pr_number}/comments"
            "?per_page=100"
        ),
        token=token,
    )
    if not isinstance(comments_payload, list):
        raise ValueError("GitHub issue comments response must be a list.")
    existing_comment = next(
        (
            cast(dict[str, object], item)
            for item in comments_payload
            if isinstance(item, dict)
            and MANAGER_PREVIEW_APPROVAL_COMMENT_MARKER in str(item.get("body") or "")
            and _github_actor_id(item.get("user")) == authenticated_user_id
        ),
        None,
    )
    if existing_comment is None:
        comment_payload = api_request(
            path=f"/repos/{quote(owner)}/{quote(repo)}/issues/{projection.pr_number}/comments",
            token=token,
            method="POST",
            body={"body": projection.comment_markdown},
        )
    else:
        raw_comment_id = existing_comment.get("id")
        comment_id = (
            raw_comment_id
            if isinstance(raw_comment_id, int) and not isinstance(raw_comment_id, bool)
            else 0
        )
        if comment_id < 1:
            raise ValueError("GitHub manager preview approval comment is missing its id.")
        comment_payload = api_request(
            path=f"/repos/{quote(owner)}/{quote(repo)}/issues/comments/{comment_id}",
            token=token,
            method="PATCH",
            body={"body": projection.comment_markdown},
        )
    if not isinstance(comment_payload, dict):
        raise ValueError("GitHub manager preview approval comment response must be an object.")
    target_url = str(comment_payload.get("html_url") or projection.preview_url or projection.pr_url)
    status_payload = api_request(
        path=(
            f"/repos/{quote(owner)}/{quote(repo)}/statuses/{quote(projection.head_sha, safe='')}"
        ),
        token=token,
        method="POST",
        body={
            "state": projection.check_state,
            "target_url": target_url,
            "description": projection.check_description,
            "context": MANAGER_PREVIEW_APPROVAL_CHECK_NAME,
        },
    )
    if not isinstance(status_payload, dict):
        raise ValueError("GitHub manager preview approval status response must be an object.")
    return {
        "comment_url": target_url,
        "check_state": projection.check_state,
        "check_context": MANAGER_PREVIEW_APPROVAL_CHECK_NAME,
    }


def _github_actor_id(value: object) -> int:
    if not isinstance(value, dict):
        return 0
    raw_id = value.get("id")
    if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0:
        return raw_id
    return 0


def _unavailable_projection(
    *,
    required: bool,
    product: str,
    context: str,
    repository: str,
    pr_number: int,
    pr_url: str,
    pr_state: str,
    head_sha: str,
    reason: str,
    evaluated_at: str,
    check_state: ManagerPreviewApprovalCheckState = "error",
    preview: PreviewRecord | None = None,
) -> ManagerPreviewApprovalProjection:
    decision = ManagerPreviewApprovalDecision(
        status="unavailable",
        reason_code="policy_unavailable" if "policy" in reason.lower() else "preview_inactive",
        reason=reason,
        evaluated_at=evaluated_at,
    )
    return ManagerPreviewApprovalProjection(
        required=required,
        product=product,
        context=context,
        repository=repository,
        pr_number=pr_number,
        pr_url=pr_url,
        pr_state=pr_state,
        head_sha=head_sha,
        preview_id=preview.preview_id if preview is not None else "",
        preview_url=preview.canonical_url if preview is not None else "",
        decision=decision,
        check_state=check_state,
        check_description=(
            "Manager preview approval is not required."
            if not required and check_state == "success"
            else "Manager preview approval is unavailable."
        ),
        comment_markdown=_render_unavailable_comment(reason=reason),
    )


def _stale_decision(
    *,
    binding: ManagerPreviewApprovalBinding | None,
    evaluated_at: str,
    reason: str,
) -> ManagerPreviewApprovalDecision:
    return ManagerPreviewApprovalDecision(
        status="stale",
        reason_code="approval_stale",
        reason=reason,
        current_binding_sha256=binding.binding_sha256 if binding is not None else "",
        evaluated_at=evaluated_at,
    )


def _check_state(decision: ManagerPreviewApprovalDecision) -> ManagerPreviewApprovalCheckState:
    if decision.status == "approved":
        return "success"
    if decision.status == "pending":
        return "pending"
    if decision.status == "unavailable":
        return "error"
    return "failure"


def _check_description(decision: ManagerPreviewApprovalDecision) -> str:
    descriptions = {
        "approved": "The manager approved the exact current preview.",
        "pending": "Waiting for the manager to approve the exact current preview.",
        "changes_requested": "The manager requested changes to the current preview.",
        "revoked": "The manager revoked approval for the current preview.",
        "stale": "Manager approval is stale for the current preview.",
        "unavailable": "Manager preview approval is unavailable.",
    }
    return descriptions[decision.status]


def _render_comment(
    *,
    preview: PreviewRecord,
    generation: PreviewGenerationRecord,
    binding: ManagerPreviewApprovalBinding | None,
    decision: ManagerPreviewApprovalDecision,
) -> str:
    lines = [
        MANAGER_PREVIEW_APPROVAL_COMMENT_MARKER,
        "## Manager preview approval",
        "",
        f"- State: **{decision.status.replace('_', ' ')}**",
        f"- Preview URL: {preview.canonical_url}",
        f"- PR head: `{generation.anchor_summary.head_sha}`",
        f"- Serving generation: `{generation.generation_id}`",
        f"- Artifact: `{generation.artifact_id}`",
        f"- Manifest: `{generation.resolved_manifest_fingerprint}`",
        f"- Reason: {decision.reason}",
    ]
    if binding is not None:
        lines.extend(
            [
                f"- Immutable image digest: `{binding.artifact_image_digest}`",
                f"- Exact fingerprint: `{binding.binding_sha256}`",
                "",
                "The authorized manager can use one exact command:",
                "",
                f"- `/preview approve {binding.binding_sha256}`",
                f"- `/preview changes {binding.binding_sha256} <reason>`",
                f"- `/preview revoke {binding.binding_sha256} <reason>`",
            ]
        )
    return "\n".join(lines)


def _render_unavailable_comment(*, reason: str) -> str:
    return "\n".join(
        [
            MANAGER_PREVIEW_APPROVAL_COMMENT_MARKER,
            "## Manager preview approval",
            "",
            "- State: **unavailable**",
            f"- Reason: {reason}",
            "",
            "No manager command is valid until Launchplane can resolve exact preview evidence.",
        ]
    )
