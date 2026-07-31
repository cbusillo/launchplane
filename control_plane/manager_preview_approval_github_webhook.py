from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
from typing import Protocol, cast

import click

from control_plane.contracts.manager_preview_approval_projection import (
    ManagerPreviewApprovalCommand,
    ManagerPreviewApprovalCommandAction,
    ManagerPreviewApprovalProjection,
)
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.durable_operation_authorization import read_active_authz_policy_record
from control_plane.manager_preview_approval import (
    ManagerPreviewApprovalAuthorizationError,
    ManagerPreviewApprovalEvidenceError,
    ManagerPreviewApprovalEventConflictError,
    ManagerPreviewApprovalEventStore,
    build_current_manager_preview_approval_binding,
    build_manager_preview_approval_system_event,
    manager_preview_approval_required,
    record_manager_preview_approval_event,
)
from control_plane.manager_preview_approval_projection import (
    GitHubApiRequest,
    ManagerPreviewApprovalProjectionStore,
    build_manager_preview_approval_projection,
    write_manager_preview_approval_projection,
)
from control_plane.service_auth import GitHubHumanIdentity
from control_plane.workflows.launchplane import (
    github_api_request,
    resolve_launchplane_github_token,
    verify_github_webhook_signature,
)


MANAGER_PREVIEW_APPROVAL_WEBHOOK_ROUTE = "/v1/manager-preview-approval/github-webhook"
MANAGER_PREVIEW_APPROVAL_RECONCILE_ROUTE = "/v1/manager-preview-approval/reconcile"
_WEBHOOK_SECRET_ENV_KEY = "LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET"
_COMMAND_PATTERN = re.compile(
    r"^/preview\s+(?P<command>approve|changes|revoke)\s+"
    r"(?P<fingerprint>[0-9a-fA-F]{64})(?:\s+(?P<reason>.+))?$"
)
_LOGGER = logging.getLogger(__name__)


class ManagerPreviewApprovalGitHubStore(
    ManagerPreviewApprovalProjectionStore,
    ManagerPreviewApprovalEventStore,
    Protocol,
):
    pass


class _WebhookSignatureVerifier(Protocol):
    def __call__(
        self,
        *,
        payload_bytes: bytes,
        signature_header: str,
        secret: str,
    ) -> None: ...


class _GitHubTokenResolver(Protocol):
    def __call__(self, *, control_plane_root: Path, context_name: str) -> str: ...


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _webhook_secret() -> str:
    return os.environ.get(_WEBHOOK_SECRET_ENV_KEY, "").strip()


@dataclass(frozen=True)
class ManagerPreviewApprovalGitHubDependencies:
    webhook_secret: Callable[[], str] = _webhook_secret
    verify_signature: _WebhookSignatureVerifier = verify_github_webhook_signature
    now_timestamp: Callable[[], str] = _utc_now_timestamp
    github_api: GitHubApiRequest = github_api_request
    github_token: _GitHubTokenResolver = resolve_launchplane_github_token


def parse_manager_preview_approval_command(
    body: str,
) -> ManagerPreviewApprovalCommand | None:
    normalized = body.strip()
    if "\n" in normalized or "\r" in normalized:
        return None
    match = _COMMAND_PATTERN.fullmatch(normalized)
    if match is None:
        return None
    command = match.group("command").lower()
    action = cast(
        ManagerPreviewApprovalCommandAction,
        {
            "approve": "approved",
            "changes": "changes_requested",
            "revoke": "revoked",
        }[command],
    )
    reason = (match.group("reason") or "").strip()
    try:
        return ManagerPreviewApprovalCommand(
            action=action,
            fingerprint=match.group("fingerprint"),
            reason=reason,
        )
    except ValueError:
        return None


def handle_manager_preview_approval_github_webhook_request(
    payload_bytes: bytes,
    event_name: str,
    delivery_id: str,
    signature_header: str,
    record_store: object,
    control_plane_root: Path,
    trace_id: str,
    *,
    dependencies: ManagerPreviewApprovalGitHubDependencies | None = None,
) -> tuple[int, dict[str, object]]:
    resolved_dependencies = dependencies or ManagerPreviewApprovalGitHubDependencies()
    normalized_delivery_id = delivery_id.strip()
    normalized_event_name = event_name.strip().lower()
    if not normalized_delivery_id:
        return _error_response(trace_id, 400, "invalid_payload", "GitHub delivery id is required.")
    try:
        resolved_dependencies.verify_signature(
            payload_bytes=payload_bytes,
            signature_header=signature_header,
            secret=resolved_dependencies.webhook_secret(),
        )
    except click.ClickException as error:
        return _error_response(trace_id, 401, "invalid_signature", str(error))
    try:
        decoded = json.loads(payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _error_response(
            trace_id, 400, "invalid_payload", "GitHub webhook body is invalid JSON."
        )
    if not isinstance(decoded, dict):
        return _error_response(
            trace_id, 400, "invalid_payload", "GitHub webhook body must be an object."
        )
    payload = cast(dict[str, object], decoded)
    try:
        store = cast(ManagerPreviewApprovalGitHubStore, record_store)
        if normalized_event_name == "issue_comment":
            return _handle_issue_comment(
                payload=payload,
                delivery_id=normalized_delivery_id,
                trace_id=trace_id,
                record_store=store,
                control_plane_root=control_plane_root,
                dependencies=resolved_dependencies,
            )
        if normalized_event_name == "pull_request":
            return _handle_pull_request(
                payload=payload,
                delivery_id=normalized_delivery_id,
                trace_id=trace_id,
                record_store=store,
                control_plane_root=control_plane_root,
                dependencies=resolved_dependencies,
            )
        return 202, _accepted(trace_id, skipped=True, reason="unsupported_event")
    except ManagerPreviewApprovalEventConflictError as error:
        return _error_response(
            trace_id,
            409,
            "manager_preview_approval_conflict",
            str(error),
        )
    except (click.ClickException, ValueError, LookupError, FileNotFoundError) as error:
        return _error_response(
            trace_id,
            503,
            "manager_preview_approval_unavailable",
            str(error),
        )


def reconcile_manager_preview_approval_for_pr(
    *,
    repository: str,
    pr_number: int,
    record_store: ManagerPreviewApprovalGitHubStore,
    control_plane_root: Path,
    evaluated_at: str = "",
    dependencies: ManagerPreviewApprovalGitHubDependencies | None = None,
) -> dict[str, object]:
    resolved_dependencies = dependencies or ManagerPreviewApprovalGitHubDependencies()
    resolved_evaluated_at = evaluated_at.strip() or resolved_dependencies.now_timestamp()
    profile = _product_profile(record_store, repository)
    token = resolved_dependencies.github_token(
        control_plane_root=control_plane_root,
        context_name=profile.preview.context,
    )
    if not token.strip():
        raise click.ClickException("Launchplane GitHub projection credentials are unavailable.")
    pull_request = _github_pull_request(
        api_request=resolved_dependencies.github_api,
        token=token,
        repository=repository,
        pr_number=pr_number,
    )
    projection = build_manager_preview_approval_projection(
        record_store=record_store,
        repository=repository,
        pr_number=pr_number,
        pr_url=_string(pull_request, "html_url"),
        pr_state=_string(pull_request, "state"),
        current_head_sha=_nested_string(pull_request, "head", "sha"),
        evaluated_at=resolved_evaluated_at,
    )
    result = write_manager_preview_approval_projection(
        projection=projection,
        token=token,
        api_request=resolved_dependencies.github_api,
    )
    return {
        "required": projection.required,
        "status": projection.decision.status,
        "fingerprint": projection.binding_sha256,
        **result,
    }


def invalidate_manager_preview_approval_for_pr(
    *,
    repository: str,
    pr_number: int,
    reason: str,
    source_event_kind: str,
    source_event_id: str,
    record_store: ManagerPreviewApprovalGitHubStore,
    control_plane_root: Path,
    occurred_at: str = "",
    dependencies: ManagerPreviewApprovalGitHubDependencies | None = None,
) -> dict[str, object]:
    resolved_dependencies = dependencies or ManagerPreviewApprovalGitHubDependencies()
    resolved_occurred_at = occurred_at.strip() or resolved_dependencies.now_timestamp()
    profile = _product_profile(record_store, repository)
    policy_record = read_active_authz_policy_record(record_store)
    if not manager_preview_approval_required(
        policy_record=policy_record,
        product=profile.product,
        context=profile.preview.context,
    ):
        return {"required": False, "event_status": "not_required"}
    event_status: str
    try:
        preview, generation = _preview_and_generation(
            record_store=record_store,
            profile=profile,
            repository=repository,
            pr_number=pr_number,
        )
        binding = build_current_manager_preview_approval_binding(
            product=profile.product,
            preview=preview,
            generation=generation,
        )
        event_status = record_store.write_manager_preview_approval_event_record(
            build_manager_preview_approval_system_event(
                binding=binding,
                action="invalidated",
                occurred_at=resolved_occurred_at,
                source_event_kind=source_event_kind,
                source_event_id=source_event_id,
                reason=reason,
            )
        )
    except (
        FileNotFoundError,
        LookupError,
        ManagerPreviewApprovalEvidenceError,
        ValueError,
    ):
        event_status = "unavailable"
    projection = reconcile_manager_preview_approval_for_pr(
        repository=repository,
        pr_number=pr_number,
        record_store=record_store,
        control_plane_root=control_plane_root,
        evaluated_at=resolved_occurred_at,
        dependencies=resolved_dependencies,
    )
    return {"required": True, "event_status": event_status, **projection}


def reconcile_manager_preview_approval_for_pr_best_effort(
    *,
    repository: str,
    pr_number: int,
    record_store: object,
    control_plane_root: Path,
    dependencies: ManagerPreviewApprovalGitHubDependencies | None = None,
    force: bool = False,
) -> bool:
    store = cast(ManagerPreviewApprovalGitHubStore, record_store)
    if not force:
        try:
            profile = _product_profile(store, repository)
            policy_record = read_active_authz_policy_record(store)
        except (AttributeError, FileNotFoundError, LookupError, TypeError, ValueError):
            return True
        if not manager_preview_approval_required(
            policy_record=policy_record,
            product=profile.product,
            context=profile.preview.context,
        ):
            return True
    try:
        reconcile_manager_preview_approval_for_pr(
            repository=repository,
            pr_number=pr_number,
            record_store=store,
            control_plane_root=control_plane_root,
            dependencies=dependencies,
        )
    except Exception:
        _LOGGER.warning(
            "Manager preview approval reconciliation failed",
            exc_info=True,
            extra={"repository": repository, "pr_number": pr_number},
        )
        return False
    return True


def invalidate_manager_preview_approval_for_pr_best_effort(
    *,
    repository: str,
    pr_number: int,
    reason: str,
    source_event_kind: str,
    source_event_id: str,
    record_store: object,
    control_plane_root: Path,
    dependencies: ManagerPreviewApprovalGitHubDependencies | None = None,
) -> bool:
    try:
        invalidate_manager_preview_approval_for_pr(
            repository=repository,
            pr_number=pr_number,
            reason=reason,
            source_event_kind=source_event_kind,
            source_event_id=source_event_id,
            record_store=cast(ManagerPreviewApprovalGitHubStore, record_store),
            control_plane_root=control_plane_root,
            dependencies=dependencies,
        )
    except Exception:
        _LOGGER.warning(
            "Manager preview approval invalidation failed",
            exc_info=True,
            extra={"repository": repository, "pr_number": pr_number},
        )
        return False
    return True


def reconcile_all_manager_preview_approvals_best_effort(
    *,
    record_store: object,
    control_plane_root: Path,
    dependencies: ManagerPreviewApprovalGitHubDependencies | None = None,
) -> tuple[int, int]:
    store = cast(ManagerPreviewApprovalGitHubStore, record_store)
    attempted = 0
    succeeded = 0
    seen: set[tuple[str, int]] = set()
    try:
        profiles = store.list_product_profile_records()
    except (AttributeError, TypeError, ValueError):
        return attempted, succeeded
    for profile in profiles:
        repository = profile.repository.strip()
        if not _valid_repository(repository) or not profile.preview.context.strip():
            continue
        anchor_repo = repository.split("/", 1)[1]
        try:
            previews = store.list_preview_records(
                context_name=profile.preview.context,
                anchor_repo=anchor_repo,
                limit=200,
            )
        except (AttributeError, TypeError, ValueError):
            continue
        for preview in previews:
            target = (repository, preview.anchor_pr_number)
            if target in seen:
                continue
            seen.add(target)
            attempted += 1
            if reconcile_manager_preview_approval_for_pr_best_effort(
                repository=repository,
                pr_number=preview.anchor_pr_number,
                record_store=store,
                control_plane_root=control_plane_root,
                dependencies=dependencies,
                force=True,
            ):
                succeeded += 1
    return attempted, succeeded


def _handle_issue_comment(
    *,
    payload: dict[str, object],
    delivery_id: str,
    trace_id: str,
    record_store: ManagerPreviewApprovalGitHubStore,
    control_plane_root: Path,
    dependencies: ManagerPreviewApprovalGitHubDependencies,
) -> tuple[int, dict[str, object]]:
    if payload.get("action") != "created":
        return 202, _accepted(trace_id, skipped=True, reason="unsupported_action")
    repository = _nested_string(payload, "repository", "full_name")
    pr_number = _positive_int(_mapping(payload, "issue"), "number")
    comment_id = _positive_int(_mapping(payload, "comment"), "id")
    if not _valid_repository(repository) or pr_number is None or comment_id is None:
        return _error_response(
            trace_id, 400, "invalid_payload", "GitHub comment payload is incomplete."
        )
    if _mapping(_mapping(payload, "issue"), "pull_request") is None:
        return 202, _accepted(trace_id, skipped=True, reason="comment_is_not_on_pull_request")
    profile = _product_profile(record_store, repository)
    token = dependencies.github_token(
        control_plane_root=control_plane_root,
        context_name=profile.preview.context,
    )
    if not token.strip():
        raise click.ClickException("Launchplane GitHub projection credentials are unavailable.")
    comment = _github_comment(
        api_request=dependencies.github_api,
        token=token,
        repository=repository,
        comment_id=comment_id,
    )
    command = parse_manager_preview_approval_command(_string(comment, "body"))
    if command is None:
        return 202, _accepted(trace_id, skipped=True, reason="command_not_matched")
    pull_request = _github_pull_request(
        api_request=dependencies.github_api,
        token=token,
        repository=repository,
        pr_number=pr_number,
    )
    evaluated_at = dependencies.now_timestamp()
    projection_before = build_manager_preview_approval_projection(
        record_store=record_store,
        repository=repository,
        pr_number=pr_number,
        pr_url=_string(pull_request, "html_url"),
        pr_state=_string(pull_request, "state"),
        current_head_sha=_nested_string(pull_request, "head", "sha"),
        evaluated_at=evaluated_at,
    )
    if not projection_before.required:
        return 202, _accepted(trace_id, skipped=True, reason="approval_not_required")
    if (
        not projection_before.binding_sha256
        or command.fingerprint != projection_before.binding_sha256
    ):
        _write_projection_best_effort(
            projection=projection_before,
            token=token,
            dependencies=dependencies,
        )
        return 202, _accepted(trace_id, skipped=True, reason="stale_fingerprint")
    if projection_before.decision.status in {"stale", "unavailable"}:
        _write_projection_best_effort(
            projection=projection_before,
            token=token,
            dependencies=dependencies,
        )
        return 202, _accepted(trace_id, skipped=True, reason="preview_evidence_not_current")
    user = _mapping(comment, "user")
    login = _string(user, "login")
    github_id = _positive_int(user, "id")
    if github_id is None or not login or _string(user, "type").casefold() == "bot":
        return 202, _accepted(trace_id, skipped=True, reason="unauthorized_actor")
    actor = _github_actor(
        api_request=dependencies.github_api,
        token=token,
        login=login,
    )
    if _positive_int(actor, "id") != github_id:
        return 202, _accepted(trace_id, skipped=True, reason="actor_identity_mismatch")
    preview, generation = _preview_and_generation(
        record_store=record_store,
        profile=profile,
        repository=repository,
        pr_number=pr_number,
    )
    if generation.anchor_summary.head_sha != _nested_string(pull_request, "head", "sha"):
        return 202, _accepted(trace_id, skipped=True, reason="stale_head")
    policy_record = read_active_authz_policy_record(record_store)
    try:
        result = record_manager_preview_approval_event(
            record_store=record_store,
            identity=GitHubHumanIdentity(
                login=login,
                github_id=github_id,
                name=_string(actor, "name"),
                email="",
                organizations=frozenset(),
                teams=frozenset(),
                role="read_only",
            ),
            policy_record=policy_record,
            product=profile.product,
            preview=preview,
            generation=generation,
            action=command.action,
            occurred_at=evaluated_at,
            source_event_kind="github_issue_comment",
            source_event_id=f"{repository}#{comment_id}",
            reason=command.reason,
        )
    except ManagerPreviewApprovalAuthorizationError:
        return 202, _accepted(trace_id, skipped=True, reason="unauthorized_actor")
    projection_after = build_manager_preview_approval_projection(
        record_store=record_store,
        repository=repository,
        pr_number=pr_number,
        pr_url=_string(pull_request, "html_url"),
        pr_state=_string(pull_request, "state"),
        current_head_sha=_nested_string(pull_request, "head", "sha"),
        evaluated_at=evaluated_at,
    )
    projection_result = write_manager_preview_approval_projection(
        projection=projection_after,
        token=token,
        api_request=dependencies.github_api,
    )
    return 202, _accepted(
        trace_id,
        result={
            "event_id": result.record.event_id,
            "event_status": result.status,
            "approval_status": projection_after.decision.status,
            **projection_result,
        },
        github_delivery_id=delivery_id,
    )


def _handle_pull_request(
    *,
    payload: dict[str, object],
    delivery_id: str,
    trace_id: str,
    record_store: ManagerPreviewApprovalGitHubStore,
    control_plane_root: Path,
    dependencies: ManagerPreviewApprovalGitHubDependencies,
) -> tuple[int, dict[str, object]]:
    action = str(payload.get("action") or "").strip().lower()
    if action not in {"synchronize", "reopened", "closed", "unlabeled"}:
        return 202, _accepted(trace_id, skipped=True, reason="unsupported_action")
    repository = _nested_string(payload, "repository", "full_name")
    pr_number = _positive_int(_mapping(payload, "number"), "value")
    if pr_number is None:
        raw_number = payload.get("number")
        pr_number = raw_number if isinstance(raw_number, int) and raw_number > 0 else None
    if not _valid_repository(repository) or pr_number is None:
        return _error_response(
            trace_id, 400, "invalid_payload", "GitHub pull request payload is incomplete."
        )
    profile = _product_profile(record_store, repository)
    if (
        action == "unlabeled"
        and _nested_string(payload, "label", "name") != profile.preview.enable_label
    ):
        return 202, _accepted(trace_id, skipped=True, reason="unrelated_label")
    token = dependencies.github_token(
        control_plane_root=control_plane_root,
        context_name=profile.preview.context,
    )
    pull_request = _github_pull_request(
        api_request=dependencies.github_api,
        token=token,
        repository=repository,
        pr_number=pr_number,
    )
    evaluated_at = dependencies.now_timestamp()
    if action in {"closed", "unlabeled"}:
        try:
            preview, generation = _preview_and_generation(
                record_store=record_store,
                profile=profile,
                repository=repository,
                pr_number=pr_number,
            )
            binding = build_current_manager_preview_approval_binding(
                product=profile.product,
                preview=preview,
                generation=generation,
            )
            record_store.write_manager_preview_approval_event_record(
                build_manager_preview_approval_system_event(
                    binding=binding,
                    action="invalidated",
                    occurred_at=evaluated_at,
                    source_event_kind=(
                        "github_pull_request_closed"
                        if action == "closed"
                        else "github_preview_label_removed"
                    ),
                    source_event_id=f"{repository}#{pr_number}:{delivery_id}",
                    reason=(
                        "The pull request was closed."
                        if action == "closed"
                        else "The preview enablement label was removed."
                    ),
                )
            )
        except (ValueError, LookupError, FileNotFoundError):
            pass
    projection = build_manager_preview_approval_projection(
        record_store=record_store,
        repository=repository,
        pr_number=pr_number,
        pr_url=_string(pull_request, "html_url"),
        pr_state=_string(pull_request, "state"),
        current_head_sha=_nested_string(pull_request, "head", "sha"),
        evaluated_at=evaluated_at,
    )
    if not projection.required:
        return 202, _accepted(trace_id, skipped=True, reason="approval_not_required")
    result = write_manager_preview_approval_projection(
        projection=projection,
        token=token,
        api_request=dependencies.github_api,
    )
    return 202, _accepted(
        trace_id,
        result={"approval_status": projection.decision.status, **result},
        github_delivery_id=delivery_id,
    )


def _product_profile(
    record_store: ManagerPreviewApprovalGitHubStore,
    repository: str,
) -> LaunchplaneProductProfileRecord:
    matches = tuple(
        profile
        for profile in record_store.list_product_profile_records()
        if profile.repository.strip().casefold() == repository.strip().casefold()
    )
    if len(matches) != 1:
        raise LookupError("Launchplane requires one product profile for the GitHub repository.")
    return matches[0]


def _preview_and_generation(
    *,
    record_store: ManagerPreviewApprovalGitHubStore,
    profile: LaunchplaneProductProfileRecord,
    repository: str,
    pr_number: int,
) -> tuple[PreviewRecord, PreviewGenerationRecord]:
    anchor_repo = repository.split("/", 1)[1]
    previews = record_store.list_preview_records(
        context_name=profile.preview.context,
        anchor_repo=anchor_repo,
        anchor_pr_number=pr_number,
        limit=10,
    )
    if not previews or not previews[0].serving_generation_id.strip():
        raise LookupError("The pull request does not have a serving Launchplane preview.")
    preview = previews[0]
    return preview, record_store.read_preview_generation_record(preview.serving_generation_id)


def _github_comment(
    *,
    api_request: GitHubApiRequest,
    token: str,
    repository: str,
    comment_id: int,
) -> dict[str, object]:
    payload = api_request(
        path=f"/repos/{repository}/issues/comments/{comment_id}",
        token=token,
    )
    if not isinstance(payload, dict):
        raise click.ClickException("GitHub comment response must be an object.")
    return cast(dict[str, object], payload)


def _github_pull_request(
    *,
    api_request: GitHubApiRequest,
    token: str,
    repository: str,
    pr_number: int,
) -> dict[str, object]:
    payload = api_request(
        path=f"/repos/{repository}/pulls/{pr_number}",
        token=token,
    )
    if not isinstance(payload, dict):
        raise click.ClickException("GitHub pull request response must be an object.")
    return cast(dict[str, object], payload)


def _github_actor(
    *,
    api_request: GitHubApiRequest,
    token: str,
    login: str,
) -> dict[str, object]:
    payload = api_request(path=f"/users/{login}", token=token)
    if not isinstance(payload, dict):
        raise click.ClickException("GitHub actor response must be an object.")
    return cast(dict[str, object], payload)


def _write_projection_best_effort(
    *,
    projection: ManagerPreviewApprovalProjection,
    token: str,
    dependencies: ManagerPreviewApprovalGitHubDependencies,
) -> None:
    try:
        write_manager_preview_approval_projection(
            projection=projection,
            token=token,
            api_request=dependencies.github_api,
        )
    except (ValueError, click.ClickException):
        return


def _mapping(mapping: dict[str, object] | None, key: str) -> dict[str, object] | None:
    if mapping is None:
        return None
    value = mapping.get(key)
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def _string(mapping: dict[str, object] | None, key: str) -> str:
    if mapping is None:
        return ""
    value = mapping.get(key)
    return value.strip() if isinstance(value, str) else ""


def _nested_string(mapping: dict[str, object], parent: str, key: str) -> str:
    return _string(_mapping(mapping, parent), key)


def _positive_int(mapping: dict[str, object] | None, key: str) -> int | None:
    if mapping is None:
        return None
    value = mapping.get(key)
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _valid_repository(repository: str) -> bool:
    owner, separator, repo = repository.strip().partition("/")
    return bool(separator and owner and repo and "/" not in repo)


def _accepted(
    trace_id: str,
    *,
    skipped: bool = False,
    reason: str = "",
    result: dict[str, object] | None = None,
    github_delivery_id: str = "",
) -> dict[str, object]:
    payload: dict[str, object] = {"status": "accepted", "trace_id": trace_id}
    if skipped:
        payload["skipped"] = True
    if reason:
        payload["reason"] = reason
    if result is not None:
        payload["result"] = result
    if github_delivery_id:
        payload["github_delivery_id"] = github_delivery_id
    return payload


def _error_response(
    trace_id: str,
    status_code: int,
    code: str,
    message: str,
) -> tuple[int, dict[str, object]]:
    return status_code, {
        "error": {
            "code": code,
            "message": message,
            "trace_id": trace_id,
        }
    }
