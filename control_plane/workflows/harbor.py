import hashlib
import hmac
import json
import re
import tomllib
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import click
from pydantic import ValidationError

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane import release_tuples as control_plane_release_tuples
from control_plane.contracts.github_pull_request_event import GitHubPullRequestEvent
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewGenerationState,
    PreviewPullRequestSummary,
    PreviewSourceRecord,
)
from control_plane.contracts.preview_manifest import HarborResolvedPreviewManifest
from control_plane.contracts.preview_mutation_request import (
    HarborPullRequestMutationIntent,
    PreviewDestroyMutationRequest,
    PreviewGenerationIntentRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.preview_request_metadata import (
    HARBOR_PREVIEW_REQUEST_BLOCK_INFO_STRING,
    HarborPreviewRequestMetadata,
    HarborPreviewRequestParseResult,
)
from control_plane.contracts.preview_record import PreviewRecord, PreviewState
from control_plane.contracts.promotion_record import ReleaseStatus
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.ship import utc_now_timestamp

RECENT_GENERATION_LIMIT = 3
HARBOR_PREVIEW_BASE_URL_ENV_KEY = "HARBOR_PREVIEW_BASE_URL"
HARBOR_PREVIEW_ENABLE_LABEL = "harbor-preview"
HARBOR_GITHUB_TOKEN_ENV_KEY = "GITHUB_TOKEN"
HARBOR_GITHUB_WEBHOOK_SECRET_ENV_KEY = "GITHUB_WEBHOOK_SECRET"
DEFAULT_HARBOR_BASELINE_CHANNEL = "testing"
HarborPullRequestAction = str
HARBOR_TENANT_ANCHOR_CONTEXTS: dict[str, str] = {
    "tenant-cm": "cm",
    "tenant-opw": "opw",
}
HARBOR_PREVIEW_REQUEST_BLOCK_PATTERN = re.compile(
    rf"```{re.escape(HARBOR_PREVIEW_REQUEST_BLOCK_INFO_STRING)}[ \t]*\r?\n(?P<body>.*?)\r?\n```",
    flags=re.IGNORECASE | re.DOTALL,
)
GITHUB_PULL_REQUEST_URL_PATTERN = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)/?$")
HARBOR_PR_FEEDBACK_COMMENT_MARKER = "<!-- harbor-control-plane:pr-feedback -->"


def find_preview_record(
    *,
    record_store: FilesystemRecordStore,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> PreviewRecord | None:
    records = record_store.list_preview_records(
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
        limit=2,
    )
    if not records:
        return None
    return records[0]


def harbor_preview_label_enabled(*, label_names: tuple[str, ...]) -> bool:
    return HARBOR_PREVIEW_ENABLE_LABEL in {label_name.strip() for label_name in label_names}


def harbor_anchor_repo_context(*, repo: str) -> str:
    return HARBOR_TENANT_ANCHOR_CONTEXTS.get(repo.strip(), "")


def harbor_anchor_repo_eligible(*, repo: str) -> bool:
    return bool(harbor_anchor_repo_context(repo=repo))


def classify_pull_request_event_for_harbor(
    *,
    event: GitHubPullRequestEvent,
    preview: PreviewRecord | None,
) -> HarborPullRequestAction:
    preview_enabled = harbor_preview_label_enabled(label_names=event.label_names)
    if event.action == "closed":
        if preview is not None and preview.state != "destroyed":
            return "destroy_preview"
        return "ignore"

    if preview is None:
        if event.action == "labeled" and event.action_label == HARBOR_PREVIEW_ENABLE_LABEL:
            return "enable_preview"
        if event.action in {"opened", "reopened"} and preview_enabled:
            return "enable_preview"
        return "ignore"

    if preview.state == "destroyed":
        if event.action == "reopened" and preview_enabled:
            return "enable_preview"
        return "ignore"

    if not preview_enabled:
        return "ignore"
    if event.action in {"synchronize", "edited", "reopened"}:
        return "refresh_preview"
    return "ignore"


def build_pull_request_event_action_payload(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    event: GitHubPullRequestEvent,
) -> dict[str, object]:
    request_metadata = parse_preview_request_metadata(pr_body=event.pr_body)
    action, resolved_context, preview = resolve_pull_request_event_decision(
        record_store=record_store,
        event=event,
    )
    resolved_manifest = resolve_pull_request_event_manifest(
        control_plane_root=control_plane_root,
        event=event,
        resolved_context=resolved_context,
        preview=preview,
        request_metadata=request_metadata,
    )
    mutation_intent = build_pull_request_event_mutation_intent(
        event=event,
        action=action,
        resolved_context=resolved_context,
        preview=preview,
        request_metadata=request_metadata,
        resolved_manifest=resolved_manifest,
    )
    payload = {
        "event": event.model_dump(mode="json"),
        "decision": {
            "action": action,
            "anchor_repo_eligible": bool(resolved_context),
            "resolved_context": resolved_context,
            "label_enabled": harbor_preview_label_enabled(label_names=event.label_names),
            "preview_exists": preview is not None,
            "context_resolution_required": preview is None and not resolved_context,
            "manifest_resolved": resolved_manifest is not None,
        },
        "request_metadata": request_metadata.model_dump(mode="json"),
        "manifest": resolved_manifest.model_dump(mode="json") if resolved_manifest is not None else None,
        "mutation": mutation_intent.model_dump(mode="json") if mutation_intent is not None else None,
        "preview": (
            {
                "preview_id": preview.preview_id,
                "context": preview.context,
                "state": preview.state,
                "preview_label": preview.preview_label,
                "canonical_url": preview.canonical_url,
                "active_generation_id": preview.active_generation_id,
                "serving_generation_id": preview.serving_generation_id,
                "latest_generation_id": preview.latest_generation_id,
            }
            if preview is not None
            else None
        ),
    }
    payload["feedback"] = build_pull_request_feedback_payload(
        record_store=record_store,
        event=event,
        action=action,
        preview=preview,
        request_metadata=request_metadata,
        resolved_manifest=resolved_manifest,
    )
    return payload


def adapt_github_webhook_pull_request_event(
    *, event_name: str, webhook_payload: dict[str, object]
) -> GitHubPullRequestEvent:
    normalized_event_name = event_name.strip()
    if normalized_event_name != "pull_request":
        raise click.ClickException(
            f"Harbor GitHub webhook adapter only supports event_name='pull_request', got {normalized_event_name!r}."
        )

    action = _require_webhook_string(webhook_payload, "action")
    if action not in GitHubPullRequestEvent.model_fields["action"].annotation.__args__:
        raise click.ClickException(
            f"Harbor does not support GitHub pull_request action {action!r}."
        )

    repository_payload = _require_webhook_mapping(webhook_payload, "repository")
    pull_request_payload = _require_webhook_mapping(webhook_payload, "pull_request")
    label_payload = webhook_payload.get("label")
    label_mapping = label_payload if isinstance(label_payload, dict) else None
    pr_number = _require_webhook_int(webhook_payload, "number")
    labels = _webhook_pull_request_labels(pull_request_payload=pull_request_payload)
    action_label = _webhook_action_label(action=action, label_payload=label_mapping)

    adapted_payload = {
        "action": action,
        "repo": _require_webhook_string(repository_payload, "name"),
        "pr_number": pr_number,
        "pr_url": _require_webhook_string(pull_request_payload, "html_url"),
        "occurred_at": _webhook_occurred_at(
            action=action,
            pull_request_payload=pull_request_payload,
        ),
        "pr_body": _optional_webhook_string(pull_request_payload, "body"),
        "state": _require_webhook_string(pull_request_payload, "state"),
        "merged": _optional_webhook_bool(pull_request_payload, "merged"),
        "head_sha": _require_webhook_string(
            _require_webhook_mapping(pull_request_payload, "head"),
            "sha",
        ),
        "label_names": labels,
        "action_label": action_label,
    }
    try:
        return GitHubPullRequestEvent.model_validate(adapted_payload)
    except ValidationError as exc:
        raise click.ClickException(f"Invalid adapted GitHub pull request event: {exc}") from exc


def resolve_harbor_github_webhook_secret(*, control_plane_root: Path, context_name: str) -> str:
    try:
        context_values = control_plane_runtime_environments.resolve_runtime_context_values(
            control_plane_root=control_plane_root,
            context_name=context_name,
        )
    except click.ClickException:
        return ""
    return context_values.get(HARBOR_GITHUB_WEBHOOK_SECRET_ENV_KEY, "").strip()


def verify_github_webhook_signature(*, payload_bytes: bytes, signature_header: str, secret: str) -> None:
    normalized_signature = signature_header.strip()
    if not normalized_signature:
        raise click.ClickException("GitHub webhook signature header is required.")
    if not normalized_signature.startswith("sha256="):
        raise click.ClickException(
            "GitHub webhook signature must use the X-Hub-Signature-256 format 'sha256=<hex>'."
        )
    signature_value = normalized_signature.split("=", 1)[1].strip()
    if not signature_value:
        raise click.ClickException("GitHub webhook signature is missing the sha256 digest value.")
    if not secret.strip():
        raise click.ClickException("GitHub webhook verification requires a configured shared secret.")

    expected_signature = hmac.new(
        secret.encode("utf-8"),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature_value, expected_signature):
        raise click.ClickException("GitHub webhook signature verification failed.")


def build_pull_request_feedback_payload(
    *,
    record_store: FilesystemRecordStore,
    event: GitHubPullRequestEvent,
    action: HarborPullRequestAction,
    preview: PreviewRecord | None,
    request_metadata: HarborPreviewRequestParseResult,
    resolved_manifest: HarborResolvedPreviewManifest | None,
    apply_result: dict[str, object] | None = None,
) -> dict[str, object]:
    current_preview = find_preview_record(
        record_store=record_store,
        context_name="",
        anchor_repo=event.repo,
        anchor_pr_number=event.pr_number,
    )
    if current_preview is None:
        current_preview = preview

    preview_status_payload = None
    if current_preview is not None:
        preview_status_payload = build_preview_status_payload(
            record_store=record_store,
            context_name=current_preview.context,
            anchor_repo=current_preview.anchor_repo,
            anchor_pr_number=current_preview.anchor_pr_number,
        )

    apply_state, apply_reason = _feedback_apply_outcome(apply_result=apply_result)
    canonical_url = _feedback_preview_value(
        preview_status_payload=preview_status_payload,
        preview=current_preview,
        section="preview",
        key="canonical_url",
    )
    preview_label = _feedback_preview_value(
        preview_status_payload=preview_status_payload,
        preview=current_preview,
        section="preview",
        key="preview_label",
    )
    preview_state = _feedback_preview_value(
        preview_status_payload=preview_status_payload,
        preview=current_preview,
        section="preview",
        key="state",
    )
    manifest_fingerprint = _feedback_manifest_value(
        preview_status_payload=preview_status_payload,
        resolved_manifest=resolved_manifest,
        key="resolved_manifest_fingerprint",
    )
    baseline_release_tuple_id = _feedback_manifest_value(
        preview_status_payload=preview_status_payload,
        resolved_manifest=resolved_manifest,
        key="baseline_release_tuple_id",
    )
    source_summary = _feedback_source_summary(
        preview_status_payload=preview_status_payload,
        event=event,
        request_metadata=request_metadata,
    )
    status, headline, detail = _feedback_status_summary(
        action=action,
        preview_state=preview_state,
        apply_state=apply_state,
        apply_reason=apply_reason,
        request_metadata=request_metadata,
        source_summary=source_summary,
        resolved_manifest=resolved_manifest,
    )
    return {
        "status": status,
        "headline": headline,
        "detail": detail,
        "apply_state": apply_state,
        "apply_reason": apply_reason,
        "preview_state": preview_state,
        "preview_label": preview_label,
        "canonical_url": canonical_url,
        "manifest_fingerprint": manifest_fingerprint,
        "baseline_release_tuple_id": baseline_release_tuple_id,
        "source_summary": source_summary,
        "comment_markdown": _render_pull_request_feedback_markdown(
            headline=headline,
            detail=detail,
            canonical_url=canonical_url,
            preview_label=preview_label,
            preview_state=preview_state,
            manifest_fingerprint=manifest_fingerprint,
            baseline_release_tuple_id=baseline_release_tuple_id,
            source_summary=source_summary,
            apply_state=apply_state,
            apply_reason=apply_reason,
        ),
    }


def deliver_pull_request_feedback(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    event: GitHubPullRequestEvent,
    resolved_context: str,
    feedback_payload: dict[str, object],
) -> dict[str, object]:
    github_reference = github_pull_request_reference(pr_url=event.pr_url)
    if github_reference is None:
        return {
            "delivered": False,
            "reason": "github_pull_request_reference_missing",
        }

    current_preview = find_preview_record(
        record_store=record_store,
        context_name="",
        anchor_repo=event.repo,
        anchor_pr_number=event.pr_number,
    )
    effective_context = current_preview.context if current_preview is not None else resolved_context.strip()
    if not effective_context:
        return {
            "delivered": False,
            "reason": "github_context_missing",
        }

    github_token = resolve_harbor_github_token(
        control_plane_root=control_plane_root,
        context_name=effective_context,
    )
    if not github_token:
        return {
            "delivered": False,
            "reason": "github_token_missing",
        }

    comment_body = feedback_payload.get("comment_markdown")
    if not isinstance(comment_body, str) or not comment_body.strip():
        return {
            "delivered": False,
            "reason": "feedback_comment_missing",
        }

    existing_comment = find_github_issue_comment_by_marker(
        owner=github_reference["owner"],
        repo=github_reference["repo"],
        issue_number=github_reference["pr_number"],
        token=github_token,
        marker=HARBOR_PR_FEEDBACK_COMMENT_MARKER,
    )
    if existing_comment is not None:
        comment_id = existing_comment.get("id")
        if not isinstance(comment_id, int):
            raise click.ClickException("Existing Harbor PR feedback comment is missing a numeric id.")
        updated_comment = update_github_issue_comment(
            owner=github_reference["owner"],
            repo=github_reference["repo"],
            comment_id=comment_id,
            token=github_token,
            body=comment_body,
        )
        return {
            "delivered": True,
            "action": "updated_comment",
            "comment_id": comment_id,
            "comment_url": _github_comment_url(updated_comment),
        }

    created_comment = create_github_issue_comment(
        owner=github_reference["owner"],
        repo=github_reference["repo"],
        issue_number=github_reference["pr_number"],
        token=github_token,
        body=comment_body,
    )
    created_comment_id = created_comment.get("id")
    return {
        "delivered": True,
        "action": "created_comment",
        "comment_id": created_comment_id if isinstance(created_comment_id, int) else 0,
        "comment_url": _github_comment_url(created_comment),
    }


def _feedback_apply_outcome(*, apply_result: dict[str, object] | None) -> tuple[str, str]:
    if not isinstance(apply_result, dict):
        return "not_requested", ""
    if apply_result.get("applied") is True:
        return "applied", ""
    reason = apply_result.get("reason")
    return "noop", reason if isinstance(reason, str) else ""


def _feedback_preview_value(
    *,
    preview_status_payload: dict[str, object] | None,
    preview: PreviewRecord | None,
    section: str,
    key: str,
) -> str:
    if preview_status_payload is not None:
        payload_section = preview_status_payload.get(section)
        if isinstance(payload_section, dict):
            value = payload_section.get(key)
            if isinstance(value, str):
                return value
    if preview is None:
        return ""
    value = getattr(preview, key, "")
    return value if isinstance(value, str) else ""


def _feedback_manifest_value(
    *,
    preview_status_payload: dict[str, object] | None,
    resolved_manifest: HarborResolvedPreviewManifest | None,
    key: str,
) -> str:
    if preview_status_payload is not None:
        input_summary = preview_status_payload.get("input_summary")
        if isinstance(input_summary, dict):
            value = input_summary.get(key)
            if isinstance(value, str):
                return value
    if resolved_manifest is None:
        return ""
    value = getattr(resolved_manifest, key, "")
    return value if isinstance(value, str) else ""


def _feedback_source_summary(
    *,
    preview_status_payload: dict[str, object] | None,
    event: GitHubPullRequestEvent,
    request_metadata: HarborPreviewRequestParseResult,
) -> dict[str, object]:
    if preview_status_payload is not None:
        input_summary = preview_status_payload.get("input_summary")
        if isinstance(input_summary, dict):
            anchor = input_summary.get("anchor")
            companions = input_summary.get("companions")
            if isinstance(anchor, dict) and isinstance(companions, list):
                return {
                    "anchor": anchor,
                    "companions": companions,
                }

    companion_requests = []
    if request_metadata.metadata is not None:
        companion_requests = [
            {
                "repo": companion.repo,
                "pr_number": companion.pr_number,
                "head_sha": "",
                "pr_url": "",
            }
            for companion in request_metadata.metadata.companions
        ]
    return {
        "anchor": {
            "repo": event.repo,
            "pr_number": event.pr_number,
            "head_sha": event.head_sha,
            "pr_url": event.pr_url,
        },
        "companions": companion_requests,
    }


def _feedback_status_summary(
    *,
    action: HarborPullRequestAction,
    preview_state: str,
    apply_state: str,
    apply_reason: str,
    request_metadata: HarborPreviewRequestParseResult,
    source_summary: dict[str, object],
    resolved_manifest: HarborResolvedPreviewManifest | None,
) -> tuple[str, str, str]:
    anchor = source_summary.get("anchor")
    repo = anchor.get("repo", "") if isinstance(anchor, dict) else ""
    pr_number = anchor.get("pr_number", "") if isinstance(anchor, dict) else ""
    anchor_ref = f"{repo}#{pr_number}" if repo and pr_number else "this pull request"

    if preview_state == "destroyed":
        return (
            "preview_destroyed",
            f"Harbor preview closed for {anchor_ref}.",
            "The stored Harbor preview was destroyed and its retained evidence remains available.",
        )
    if apply_state == "applied":
        return (
            "preview_updated",
            f"Harbor preview updated for {anchor_ref}.",
            "The reviewer-facing preview record now reflects the latest exact Harbor inputs.",
        )
    if apply_state == "noop" and apply_reason == "manifest_resolution_required":
        if request_metadata.status == "invalid":
            return (
                "preview_unresolved",
                f"Harbor could not apply a preview for {anchor_ref}.",
                "The Harbor preview request metadata is invalid, so Harbor kept the event fail-closed instead of guessing build inputs.",
            )
        companions = source_summary.get("companions")
        if isinstance(companions, list) and companions:
            return (
                "preview_unresolved",
                f"Harbor could not apply a preview for {anchor_ref}.",
                "Harbor could not prove the exact companion pull request head SHA, so the preview path stayed an explicit no-op.",
            )
        return (
            "preview_unresolved",
            f"Harbor could not apply a preview for {anchor_ref}.",
            "Harbor could not resolve the exact preview manifest, so the preview path stayed an explicit no-op.",
        )
    if action == "ignore":
        return (
            "no_action",
            f"Harbor took no preview action for {anchor_ref}.",
            "This pull request event did not meet the current Harbor trigger and eligibility rules.",
        )
    if request_metadata.status == "invalid":
        return (
            "preview_unresolved",
            f"Harbor could not resolve preview inputs for {anchor_ref}.",
            "The Harbor preview request metadata is invalid, so Harbor did not produce preview build truth from it.",
        )
    if resolved_manifest is not None:
        return (
            "preview_resolved",
            f"Harbor resolved preview inputs for {anchor_ref}.",
            "The exact preview manifest is ready, but this run did not apply the Harbor mutation helpers.",
        )
    return (
        "preview_unresolved",
        f"Harbor could not resolve preview inputs for {anchor_ref}.",
        "Harbor kept the event fail-closed because it could not prove the exact preview inputs yet.",
    )


def _render_pull_request_feedback_markdown(
    *,
    headline: str,
    detail: str,
    canonical_url: str,
    preview_label: str,
    preview_state: str,
    manifest_fingerprint: str,
    baseline_release_tuple_id: str,
    source_summary: dict[str, object],
    apply_state: str,
    apply_reason: str,
) -> str:
    lines = [HARBOR_PR_FEEDBACK_COMMENT_MARKER, "", headline, "", detail, ""]
    if canonical_url:
        lines.append(f"- Preview URL: {canonical_url}")
    if preview_label:
        lines.append(f"- Preview label: `{preview_label}`")
    if preview_state:
        lines.append(f"- Preview state: `{preview_state}`")
    if manifest_fingerprint:
        lines.append(f"- Manifest fingerprint: `{manifest_fingerprint}`")
    if baseline_release_tuple_id:
        lines.append(f"- Baseline release tuple: `{baseline_release_tuple_id}`")

    anchor = source_summary.get("anchor")
    if isinstance(anchor, dict):
        lines.append(f"- Anchor input: {_format_feedback_pr_summary(anchor)}")

    companions = source_summary.get("companions")
    if isinstance(companions, list) and companions:
        companion_summary = ", ".join(
            _format_feedback_pr_summary(item)
            for item in companions
            if isinstance(item, dict)
        )
        lines.append(f"- Companion inputs: {companion_summary}")

    if apply_state == "applied":
        lines.append("- Apply outcome: Harbor updated stored preview state.")
    elif apply_state == "noop":
        if apply_reason:
            lines.append(f"- Apply outcome: no-op (`{apply_reason}`)")
        else:
            lines.append("- Apply outcome: no-op")
    else:
        lines.append("- Apply outcome: dry-run only")
    return "\n".join(lines)


def _format_feedback_pr_summary(item: dict[str, object]) -> str:
    repo = item.get("repo")
    pr_number = item.get("pr_number")
    head_sha = item.get("head_sha")
    if not isinstance(repo, str) or not repo.strip():
        return "unknown"
    ref = f"`{repo}#{pr_number}`" if isinstance(pr_number, int) else f"`{repo}`"
    if isinstance(head_sha, str) and head_sha.strip():
        return f"{ref} at `{head_sha[:12]}`"
    return ref


def _require_webhook_mapping(payload: dict[str, object], key: str) -> dict[str, object]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise click.ClickException(f"GitHub webhook payload requires object field {key!r}.")
    return value


def _require_webhook_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise click.ClickException(f"GitHub webhook payload requires string field {key!r}.")
    return value.strip()


def _optional_webhook_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        return ""
    return value.strip()


def _require_webhook_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or value < 1:
        raise click.ClickException(f"GitHub webhook payload requires positive integer field {key!r}.")
    return value


def _optional_webhook_bool(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    return value if isinstance(value, bool) else False


def _webhook_pull_request_labels(*, pull_request_payload: dict[str, object]) -> tuple[str, ...]:
    labels_payload = pull_request_payload.get("labels")
    if not isinstance(labels_payload, list):
        return ()
    label_names: list[str] = []
    for item in labels_payload:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if isinstance(name, str) and name.strip():
            label_names.append(name.strip())
    return tuple(label_names)


def _webhook_action_label(*, action: str, label_payload: dict[str, object] | None) -> str:
    if action not in {"labeled", "unlabeled"} or label_payload is None:
        return ""
    name = label_payload.get("name")
    if not isinstance(name, str) or not name.strip():
        raise click.ClickException(
            f"GitHub pull_request action {action!r} requires label.name in the webhook payload."
        )
    return name.strip()


def _webhook_occurred_at(*, action: str, pull_request_payload: dict[str, object]) -> str:
    if action == "opened":
        return _optional_webhook_string(pull_request_payload, "created_at")
    if action == "closed":
        return (
            _optional_webhook_string(pull_request_payload, "closed_at")
            or _optional_webhook_string(pull_request_payload, "merged_at")
            or _optional_webhook_string(pull_request_payload, "updated_at")
        )
    return (
        _optional_webhook_string(pull_request_payload, "updated_at")
        or _optional_webhook_string(pull_request_payload, "created_at")
    )


def github_pull_request_reference(pr_url: str) -> dict[str, object] | None:
    parsed_url = urlparse(pr_url)
    if parsed_url.netloc.strip().lower() != "github.com":
        return None
    match = GITHUB_PULL_REQUEST_URL_PATTERN.match(parsed_url.path)
    if match is None:
        return None
    try:
        pr_number = int(match.group("number"))
    except ValueError:
        return None
    return {
        "owner": match.group("owner").strip(),
        "repo": match.group("repo").strip(),
        "pr_number": pr_number,
    }


def github_pr_owner(*, pr_url: str) -> str:
    reference = github_pull_request_reference(pr_url)
    if reference is None:
        return ""
    owner = reference.get("owner")
    return owner if isinstance(owner, str) else ""


def find_github_issue_comment_by_marker(
    *, owner: str, repo: str, issue_number: int, token: str, marker: str
) -> dict[str, object] | None:
    payload = github_api_request(
        path=f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        token=token,
    )
    if not isinstance(payload, list):
        raise click.ClickException(
            f"GitHub issue comments response for {owner}/{repo}#{issue_number} must be a list."
        )
    for item in payload:
        if not isinstance(item, dict):
            continue
        body = item.get("body")
        if isinstance(body, str) and marker in body:
            return item
    return None


def create_github_issue_comment(
    *, owner: str, repo: str, issue_number: int, token: str, body: str
) -> dict[str, object]:
    payload = github_api_request(
        path=f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
        token=token,
        method="POST",
        body={"body": body},
    )
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"GitHub comment create response for {owner}/{repo}#{issue_number} must be an object."
        )
    return payload


def update_github_issue_comment(
    *, owner: str, repo: str, comment_id: int, token: str, body: str
) -> dict[str, object]:
    payload = github_api_request(
        path=f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
        token=token,
        method="PATCH",
        body={"body": body},
    )
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"GitHub comment update response for {owner}/{repo} comment {comment_id} must be an object."
        )
    return payload


def github_api_request(
    *, path: str, token: str, method: str = "GET", body: dict[str, object] | None = None
) -> object:
    request_body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if body is not None:
        request_body = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(
        url=f"https://api.github.com{path}",
        method=method,
        headers=headers,
        data=request_body,
    )
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        raise click.ClickException(f"GitHub API request failed for {path}: {exc}") from exc


def _github_comment_url(comment_payload: dict[str, object]) -> str:
    html_url = comment_payload.get("html_url")
    if isinstance(html_url, str):
        return html_url.strip()
    return ""


def resolve_pull_request_event_decision(
    *,
    record_store: FilesystemRecordStore,
    event: GitHubPullRequestEvent,
) -> tuple[HarborPullRequestAction, str, PreviewRecord | None]:
    resolved_context = harbor_anchor_repo_context(repo=event.repo)
    preview = find_preview_record(
        record_store=record_store,
        context_name="",
        anchor_repo=event.repo,
        anchor_pr_number=event.pr_number,
    )
    if preview is None and not resolved_context:
        action = "ignore"
    else:
        action = classify_pull_request_event_for_harbor(event=event, preview=preview)
    return action, resolved_context, preview


def build_pull_request_event_mutation_intent(
    *,
    event: GitHubPullRequestEvent,
    action: HarborPullRequestAction,
    resolved_context: str,
    preview: PreviewRecord | None,
    request_metadata: HarborPreviewRequestParseResult,
    resolved_manifest: HarborResolvedPreviewManifest | None,
) -> HarborPullRequestMutationIntent | None:
    effective_context = preview.context if preview is not None else resolved_context
    if action in {"enable_preview", "refresh_preview"}:
        if not effective_context:
            return None
        occurred_at = _pull_request_event_timestamp(event=event)
        preview_request = PreviewMutationRequest(
            context=effective_context,
            anchor_repo=event.repo,
            anchor_pr_number=event.pr_number,
            anchor_pr_url=event.pr_url,
            created_at=occurred_at if preview is None else "",
            updated_at=occurred_at,
            eligible_at=occurred_at if preview is None else "",
        )
        if resolved_manifest is not None:
            generation_request = PreviewGenerationMutationRequest(
                context=effective_context,
                anchor_repo=event.repo,
                anchor_pr_number=event.pr_number,
                anchor_pr_url=event.pr_url,
                anchor_head_sha=event.head_sha,
                state="resolving",
                requested_reason=_pull_request_event_generation_reason(action=action),
                requested_at=occurred_at,
                resolved_manifest_fingerprint=resolved_manifest.resolved_manifest_fingerprint,
                baseline_release_tuple_id=resolved_manifest.baseline_release_tuple_id,
                source_map=resolved_manifest.source_map,
                companion_summaries=resolved_manifest.companion_summaries,
            )
            return HarborPullRequestMutationIntent(
                command="request-generation",
                preview_request=preview_request,
                generation_request=generation_request,
            )
        generation_request_seed = PreviewGenerationIntentRequest(
            context=effective_context,
            anchor_repo=event.repo,
            anchor_pr_number=event.pr_number,
            anchor_pr_url=event.pr_url,
            anchor_head_sha=event.head_sha,
            state="resolving",
            requested_reason=_pull_request_event_generation_reason(action=action),
            requested_at=occurred_at,
        )
        return HarborPullRequestMutationIntent(
            command="request-generation",
            manifest_resolution_required=True,
            preview_request=preview_request,
            generation_request_seed=generation_request_seed,
        )
    if action == "destroy_preview" and preview is not None:
        return HarborPullRequestMutationIntent(
            command="destroy-preview",
            destroy_request=PreviewDestroyMutationRequest(
                context=preview.context,
                anchor_repo=preview.anchor_repo,
                anchor_pr_number=preview.anchor_pr_number,
                destroyed_at=_pull_request_event_timestamp(event=event),
                destroy_reason=_pull_request_event_destroy_reason(event=event),
            ),
        )
    return None


def parse_preview_request_metadata(*, pr_body: str) -> HarborPreviewRequestParseResult:
    if not pr_body.strip():
        return HarborPreviewRequestParseResult(status="missing")
    block_matches = [match.group("body") for match in HARBOR_PREVIEW_REQUEST_BLOCK_PATTERN.finditer(pr_body)]
    if not block_matches:
        return HarborPreviewRequestParseResult(status="missing")
    if len(block_matches) > 1:
        return HarborPreviewRequestParseResult(
            status="invalid",
            error="Harbor preview request metadata must use exactly one fenced block.",
        )
    try:
        payload = tomllib.loads(block_matches[0])
        metadata = HarborPreviewRequestMetadata.model_validate(payload)
    except (tomllib.TOMLDecodeError, ValidationError, ValueError) as exc:
        return HarborPreviewRequestParseResult(status="invalid", error=str(exc))
    return HarborPreviewRequestParseResult(status="valid", metadata=metadata)


def resolve_pull_request_event_manifest(
    *,
    control_plane_root: Path,
    event: GitHubPullRequestEvent,
    resolved_context: str,
    preview: PreviewRecord | None,
    request_metadata: HarborPreviewRequestParseResult,
) -> HarborResolvedPreviewManifest | None:
    effective_context = preview.context if preview is not None else resolved_context
    if not effective_context:
        return None
    metadata = _resolved_preview_request_metadata(request_metadata=request_metadata)
    if metadata is None:
        return None
    try:
        release_tuple = control_plane_release_tuples.resolve_release_tuple(
            control_plane_root=control_plane_root,
            context_name=effective_context,
            channel_name=metadata.baseline_channel,
        )
    except click.ClickException:
        return None
    companion_sources, companion_summaries = _resolve_companion_sources(
        control_plane_root=control_plane_root,
        context_name=effective_context,
        anchor_pr_url=event.pr_url,
        metadata=metadata,
    )
    if metadata.companions and companion_sources is None:
        return None
    source_map = tuple(
        [
            PreviewSourceRecord(repo=event.repo, git_sha=event.head_sha, selection="anchor"),
            *(companion_sources or ()),
            *[
                PreviewSourceRecord(repo=repo_name, git_sha=git_sha, selection="baseline")
                for repo_name, git_sha in sorted(release_tuple.repo_shas.items())
                if repo_name != event.repo and repo_name not in {item.repo for item in (companion_sources or ())}
            ],
        ]
    )
    return HarborResolvedPreviewManifest(
        context=effective_context,
        baseline_channel=metadata.baseline_channel,
        baseline_release_tuple_id=release_tuple.tuple_id,
        resolved_manifest_fingerprint=generate_preview_manifest_fingerprint(
            baseline_channel=metadata.baseline_channel,
            baseline_release_tuple_id=release_tuple.tuple_id,
            source_map=source_map,
        ),
        source_map=source_map,
        companion_summaries=companion_summaries or (),
    )


def generate_preview_manifest_fingerprint(
    *,
    baseline_channel: str,
    baseline_release_tuple_id: str,
    source_map: tuple[PreviewSourceRecord, ...],
) -> str:
    fingerprint_payload = {
        "baseline_channel": baseline_channel,
        "baseline_release_tuple_id": baseline_release_tuple_id,
        "source_map": [
            {
                "repo": item.repo,
                "git_sha": item.git_sha,
                "selection": item.selection,
            }
            for item in source_map
        ],
    }
    normalized_payload = json.dumps(fingerprint_payload, separators=(",", ":"), sort_keys=True)
    return f"harbor-manifest-{hashlib.sha256(normalized_payload.encode('utf-8')).hexdigest()[:16]}"


def _resolved_preview_request_metadata(
    *, request_metadata: HarborPreviewRequestParseResult
) -> HarborPreviewRequestMetadata | None:
    if request_metadata.status == "invalid":
        return None
    if request_metadata.metadata is not None:
        return request_metadata.metadata
    return HarborPreviewRequestMetadata(baseline_channel=DEFAULT_HARBOR_BASELINE_CHANNEL)


def _resolve_companion_sources(
    *,
    control_plane_root: Path,
    context_name: str,
    anchor_pr_url: str,
    metadata: HarborPreviewRequestMetadata,
) -> tuple[tuple[PreviewSourceRecord, ...] | None, tuple[PreviewPullRequestSummary, ...] | None]:
    if not metadata.companions:
        return (), ()
    github_owner = github_pr_owner(pr_url=anchor_pr_url)
    github_token = resolve_harbor_github_token(
        control_plane_root=control_plane_root,
        context_name=context_name,
    )
    if not github_owner or not github_token:
        return None, None
    sources: list[PreviewSourceRecord] = []
    summaries: list[PreviewPullRequestSummary] = []
    for companion in metadata.companions:
        try:
            companion_head_sha, companion_pr_url = fetch_github_pull_request_head(
                owner=github_owner,
                repo=companion.repo,
                pr_number=companion.pr_number,
                token=github_token,
            )
        except click.ClickException:
            return None, None
        sources.append(
            PreviewSourceRecord(
                repo=companion.repo,
                git_sha=companion_head_sha,
                selection="companion",
            )
        )
        summaries.append(
            PreviewPullRequestSummary(
                repo=companion.repo,
                pr_number=companion.pr_number,
                head_sha=companion_head_sha,
                pr_url=companion_pr_url,
            )
        )
    return tuple(sources), tuple(summaries)


def resolve_harbor_github_token(*, control_plane_root: Path, context_name: str) -> str:
    try:
        context_values = control_plane_runtime_environments.resolve_runtime_context_values(
            control_plane_root=control_plane_root,
            context_name=context_name,
        )
    except click.ClickException:
        return ""
    return context_values.get(HARBOR_GITHUB_TOKEN_ENV_KEY, "").strip()


def fetch_github_pull_request_head(*, owner: str, repo: str, pr_number: int, token: str) -> tuple[str, str]:
    payload = github_api_request(
        path=f"/repos/{owner}/{repo}/pulls/{pr_number}",
        token=token,
    )
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"GitHub pull request response for {owner}/{repo}#{pr_number} must be an object."
        )
    head = payload.get("head")
    if not isinstance(head, dict):
        raise click.ClickException(
            f"GitHub pull request {owner}/{repo}#{pr_number} is missing head data."
        )
    head_sha = head.get("sha")
    if not isinstance(head_sha, str) or not head_sha.strip():
        raise click.ClickException(
            f"GitHub pull request {owner}/{repo}#{pr_number} is missing head.sha."
        )
    html_url = payload.get("html_url")
    companion_pr_url = (
        html_url.strip()
        if isinstance(html_url, str) and html_url.strip()
        else f"https://github.com/{owner}/{repo}/pull/{pr_number}"
    )
    return head_sha.strip(), companion_pr_url


def _pull_request_event_timestamp(*, event: GitHubPullRequestEvent) -> str:
    return event.occurred_at.strip() or utc_now_timestamp()


def _pull_request_event_generation_reason(*, action: HarborPullRequestAction) -> str:
    return f"github_pr_event_{action}"


def _pull_request_event_destroy_reason(*, event: GitHubPullRequestEvent) -> str:
    if event.merged:
        return "pull_request_merged"
    return "pull_request_closed"


def build_preview_label(*, context_name: str, anchor_repo: str, anchor_pr_number: int) -> str:
    return f"{context_name}/{anchor_repo}/pr-{anchor_pr_number}"


def build_preview_route_path(*, context_name: str, anchor_repo: str, anchor_pr_number: int) -> str:
    return f"/previews/{context_name}/{anchor_repo}/pr-{anchor_pr_number}"


def generate_preview_id(*, context_name: str, anchor_repo: str, anchor_pr_number: int) -> str:
    preview_key = f"{context_name}-{anchor_repo}-pr-{anchor_pr_number}".lower()
    normalized_key = re.sub(r"[^a-z0-9]+", "-", preview_key).strip("-")
    return f"preview-{normalized_key}"


def generate_preview_generation_id(*, preview_id: str, sequence: int) -> str:
    return f"{preview_id}-generation-{sequence:04d}"


def resolve_harbor_preview_base_url(*, control_plane_root: Path, context_name: str) -> str:
    context_values = control_plane_runtime_environments.resolve_runtime_context_values(
        control_plane_root=control_plane_root,
        context_name=context_name,
    )
    preview_base_url = context_values.get(HARBOR_PREVIEW_BASE_URL_ENV_KEY, "").strip()
    if not preview_base_url:
        raise click.ClickException(
            f"Runtime environments file is missing {HARBOR_PREVIEW_BASE_URL_ENV_KEY} for {context_name!r}."
        )
    return preview_base_url.rstrip("/")


def build_preview_canonical_url(
    *,
    preview_base_url: str,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> str:
    normalized_base_url = preview_base_url.strip().rstrip("/")
    if not normalized_base_url:
        raise ValueError("preview canonical URL requires preview_base_url")
    return (
        f"{normalized_base_url}"
        f"{build_preview_route_path(context_name=context_name, anchor_repo=anchor_repo, anchor_pr_number=anchor_pr_number)}"
    )


def build_preview_record(
    *,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
    anchor_pr_url: str,
    created_at: str,
    updated_at: str = "",
    eligible_at: str = "",
    preview_base_url: str,
    state: PreviewState = "pending",
    preview_id: str = "",
    paused_at: str = "",
    destroy_after: str = "",
    destroyed_at: str = "",
    destroy_reason: str = "",
    active_generation_id: str = "",
    serving_generation_id: str = "",
    latest_generation_id: str = "",
    latest_manifest_fingerprint: str = "",
) -> PreviewRecord:
    resolved_preview_id = preview_id or generate_preview_id(
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    return PreviewRecord(
        preview_id=resolved_preview_id,
        context=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
        anchor_pr_url=anchor_pr_url,
        preview_label=build_preview_label(
            context_name=context_name,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
        ),
        canonical_url=build_preview_canonical_url(
            preview_base_url=preview_base_url,
            context_name=context_name,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
        ),
        state=state,
        created_at=created_at,
        updated_at=updated_at or created_at,
        eligible_at=eligible_at or created_at,
        paused_at=paused_at,
        destroy_after=destroy_after,
        destroyed_at=destroyed_at,
        destroy_reason=destroy_reason,
        active_generation_id=active_generation_id,
        serving_generation_id=serving_generation_id,
        latest_generation_id=latest_generation_id,
        latest_manifest_fingerprint=latest_manifest_fingerprint,
    )


def build_preview_generation_record(
    *,
    preview_id: str,
    sequence: int,
    state: PreviewGenerationState,
    requested_reason: str,
    requested_at: str,
    resolved_manifest_fingerprint: str,
    anchor_repo: str,
    anchor_pr_number: int,
    anchor_pr_url: str,
    anchor_head_sha: str,
    generation_id: str = "",
    started_at: str = "",
    ready_at: str = "",
    finished_at: str = "",
    superseded_at: str = "",
    failed_at: str = "",
    expires_at: str = "",
    artifact_id: str = "",
    baseline_release_tuple_id: str = "",
    source_map: tuple[PreviewSourceRecord, ...] = (),
    companion_summaries: tuple[PreviewPullRequestSummary, ...] = (),
    deploy_status: ReleaseStatus = "pending",
    verify_status: ReleaseStatus = "pending",
    overall_health_status: ReleaseStatus = "pending",
    failure_stage: str = "",
    failure_summary: str = "",
) -> PreviewGenerationRecord:
    resolved_generation_id = generation_id or generate_preview_generation_id(
        preview_id=preview_id,
        sequence=sequence,
    )
    return PreviewGenerationRecord(
        generation_id=resolved_generation_id,
        preview_id=preview_id,
        sequence=sequence,
        state=state,
        requested_reason=requested_reason,
        requested_at=requested_at,
        started_at=started_at,
        ready_at=ready_at,
        finished_at=finished_at,
        superseded_at=superseded_at,
        failed_at=failed_at,
        expires_at=expires_at,
        resolved_manifest_fingerprint=resolved_manifest_fingerprint,
        artifact_id=artifact_id,
        baseline_release_tuple_id=baseline_release_tuple_id,
        source_map=source_map,
        anchor_summary=PreviewPullRequestSummary(
            repo=anchor_repo,
            pr_number=anchor_pr_number,
            head_sha=anchor_head_sha,
            pr_url=anchor_pr_url,
        ),
        companion_summaries=companion_summaries,
        deploy_status=deploy_status,
        verify_status=verify_status,
        overall_health_status=overall_health_status,
        failure_stage=failure_stage,
        failure_summary=failure_summary,
    )


def build_preview_record_from_request(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    request: PreviewMutationRequest,
) -> PreviewRecord:
    existing_preview = find_preview_record(
        record_store=record_store,
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
    )
    resolved_created_at = request.created_at.strip() or (
        existing_preview.created_at if existing_preview is not None else ""
    )
    if not resolved_created_at:
        raise click.ClickException(
            "Preview mutation requires created_at when no existing Harbor preview is stored."
        )
    resolved_updated_at = request.updated_at.strip() or resolved_created_at
    resolved_eligible_at = request.eligible_at.strip() or (
        existing_preview.eligible_at if existing_preview is not None else resolved_created_at
    )
    resolved_preview_id = (
        existing_preview.preview_id
        if existing_preview is not None
        else generate_preview_id(
            context_name=request.context,
            anchor_repo=request.anchor_repo,
            anchor_pr_number=request.anchor_pr_number,
        )
    )
    preview_base_url = resolve_harbor_preview_base_url(
        control_plane_root=control_plane_root,
        context_name=request.context,
    )
    return build_preview_record(
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
        anchor_pr_url=request.anchor_pr_url,
        created_at=resolved_created_at,
        updated_at=resolved_updated_at,
        eligible_at=resolved_eligible_at,
        preview_base_url=preview_base_url,
        state=request.state,
        preview_id=resolved_preview_id,
        paused_at=request.paused_at,
        destroy_after=request.destroy_after,
        destroyed_at=request.destroyed_at,
        destroy_reason=request.destroy_reason,
        active_generation_id=request.active_generation_id,
        serving_generation_id=request.serving_generation_id,
        latest_generation_id=request.latest_generation_id,
        latest_manifest_fingerprint=request.latest_manifest_fingerprint,
    )


def build_preview_generation_record_from_request(
    *,
    record_store: FilesystemRecordStore,
    request: PreviewGenerationMutationRequest,
) -> PreviewGenerationRecord:
    preview = find_preview_record(
        record_store=record_store,
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
    )
    if preview is None:
        raise click.ClickException(
            f"No Harbor preview found for {request.context}/{request.anchor_repo}/pr-{request.anchor_pr_number}."
        )

    existing_generations = record_store.list_preview_generation_records(preview_id=preview.preview_id)
    existing_generation = next(
        (
            record
            for record in existing_generations
            if request.generation_id.strip() and record.generation_id == request.generation_id
        ),
        None,
    )
    if existing_generation is not None:
        resolved_sequence = request.sequence or existing_generation.sequence
        resolved_generation_id = existing_generation.generation_id
    else:
        resolved_sequence = request.sequence or _next_preview_generation_sequence(
            existing_generations=existing_generations
        )
        resolved_generation_id = request.generation_id.strip() or generate_preview_generation_id(
            preview_id=preview.preview_id,
            sequence=resolved_sequence,
        )

    return build_preview_generation_record(
        preview_id=preview.preview_id,
        sequence=resolved_sequence,
        state=request.state,
        requested_reason=request.requested_reason,
        requested_at=request.requested_at,
        resolved_manifest_fingerprint=request.resolved_manifest_fingerprint,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
        anchor_pr_url=request.anchor_pr_url,
        anchor_head_sha=request.anchor_head_sha,
        generation_id=resolved_generation_id,
        started_at=request.started_at,
        ready_at=request.ready_at,
        finished_at=request.finished_at,
        superseded_at=request.superseded_at,
        failed_at=request.failed_at,
        expires_at=request.expires_at,
        artifact_id=request.artifact_id,
        baseline_release_tuple_id=request.baseline_release_tuple_id,
        source_map=request.source_map,
        companion_summaries=request.companion_summaries,
        deploy_status=request.deploy_status,
        verify_status=request.verify_status,
        overall_health_status=request.overall_health_status,
        failure_stage=request.failure_stage,
        failure_summary=request.failure_summary,
    )


def apply_generation_requested_transition(
    *,
    preview: PreviewRecord,
    generation: PreviewGenerationRecord,
) -> PreviewRecord:
    return preview.model_copy(
        update={
            "state": "active" if preview.serving_generation_id else "pending",
            "updated_at": generation.requested_at,
            "destroyed_at": "",
            "destroy_reason": "",
            "active_generation_id": generation.generation_id,
            "latest_generation_id": generation.generation_id,
            "latest_manifest_fingerprint": generation.resolved_manifest_fingerprint,
        }
    )


def apply_generation_ready_transition(
    *,
    preview: PreviewRecord,
    generation: PreviewGenerationRecord,
) -> PreviewRecord:
    return preview.model_copy(
        update={
            "state": "active",
            "updated_at": generation.ready_at or generation.finished_at or generation.requested_at,
            "destroyed_at": "",
            "destroy_reason": "",
            "active_generation_id": generation.generation_id,
            "serving_generation_id": generation.generation_id,
            "latest_generation_id": generation.generation_id,
            "latest_manifest_fingerprint": generation.resolved_manifest_fingerprint,
        }
    )


def apply_generation_failed_transition(
    *,
    preview: PreviewRecord,
    generation: PreviewGenerationRecord,
) -> PreviewRecord:
    return preview.model_copy(
        update={
            "state": "failed",
            "updated_at": generation.failed_at or generation.finished_at or generation.requested_at,
            "active_generation_id": generation.generation_id,
            "latest_generation_id": generation.generation_id,
            "latest_manifest_fingerprint": generation.resolved_manifest_fingerprint,
        }
    )


def apply_preview_destroyed_transition(
    *,
    preview: PreviewRecord,
    destroyed_at: str,
    destroy_reason: str,
) -> PreviewRecord:
    if not destroyed_at.strip():
        raise ValueError("preview destroyed transition requires destroyed_at")
    if not destroy_reason.strip():
        raise ValueError("preview destroyed transition requires destroy_reason")
    return preview.model_copy(
        update={
            "state": "destroyed",
            "updated_at": destroyed_at,
            "destroyed_at": destroyed_at,
            "destroy_reason": destroy_reason,
            "active_generation_id": "",
            "serving_generation_id": "",
        }
    )


def build_preview_status_payload(
    *,
    record_store: FilesystemRecordStore,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> dict[str, object] | None:
    preview = find_preview_record(
        record_store=record_store,
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if preview is None:
        return None

    generations = record_store.list_preview_generation_records(preview_id=preview.preview_id)
    generations_by_id = {record.generation_id: record for record in generations}
    serving_generation = generations_by_id.get(preview.serving_generation_id)
    latest_generation = generations_by_id.get(preview.latest_generation_id)
    input_generation = serving_generation or latest_generation
    evidence_generation = serving_generation or latest_generation
    recent_generations = generations[:RECENT_GENERATION_LIMIT]
    serving_matches_latest = (
        serving_generation is not None
        and latest_generation is not None
        and serving_generation.generation_id == latest_generation.generation_id
    )

    return {
        "preview": {
            "preview_id": preview.preview_id,
            "context": preview.context,
            "anchor_repo": preview.anchor_repo,
            "anchor_pr_number": preview.anchor_pr_number,
            "anchor_pr_url": preview.anchor_pr_url,
            "preview_label": preview.preview_label,
            "canonical_url": preview.canonical_url,
            "state": preview.state,
            "created_at": preview.created_at,
            "updated_at": preview.updated_at,
            "eligible_at": preview.eligible_at,
            "paused_at": preview.paused_at,
            "destroy_after": preview.destroy_after,
            "destroyed_at": preview.destroyed_at,
            "destroy_reason": preview.destroy_reason,
        },
        "serving_generation": _generation_payload(serving_generation),
        "latest_generation": _generation_payload(latest_generation),
        "trust_summary": {
            "active_generation_id": preview.active_generation_id,
            "serving_generation_id": preview.serving_generation_id,
            "latest_generation_id": preview.latest_generation_id,
            "artifact_id": evidence_generation.artifact_id if evidence_generation is not None else "",
            "manifest_fingerprint": (
                input_generation.resolved_manifest_fingerprint if input_generation is not None else ""
            ),
            "expires_at": evidence_generation.expires_at if evidence_generation is not None else "",
            "destroy_after": preview.destroy_after,
        },
        "health_summary": {
            "overall_health_status": (
                evidence_generation.overall_health_status if evidence_generation is not None else "pending"
            ),
            "deploy_status": evidence_generation.deploy_status if evidence_generation is not None else "pending",
            "verify_status": evidence_generation.verify_status if evidence_generation is not None else "pending",
            "serving_matches_latest": serving_matches_latest,
            "status_summary": _status_summary(
                preview=preview,
                serving_generation=serving_generation,
                latest_generation=latest_generation,
            ),
        },
        "input_summary": {
            "anchor": (
                input_generation.anchor_summary.model_dump(mode="json")
                if input_generation is not None
                else {
                    "repo": preview.anchor_repo,
                    "pr_number": preview.anchor_pr_number,
                    "pr_url": preview.anchor_pr_url,
                }
            ),
            "companions": (
                [item.model_dump(mode="json") for item in input_generation.companion_summaries]
                if input_generation is not None
                else []
            ),
            "baseline_release_tuple_id": (
                input_generation.baseline_release_tuple_id if input_generation is not None else ""
            ),
            "resolved_manifest_fingerprint": (
                input_generation.resolved_manifest_fingerprint if input_generation is not None else ""
            ),
            "source_map": (
                [item.model_dump(mode="json") for item in input_generation.source_map]
                if input_generation is not None
                else []
            ),
        },
        "lifecycle_summary": {
            "state": preview.state,
            "destroy_after": preview.destroy_after,
            "destroyed_at": preview.destroyed_at,
            "destroy_reason": preview.destroy_reason,
            "next_action": _next_action(
                preview=preview,
                serving_generation=serving_generation,
                latest_generation=latest_generation,
            ),
        },
        "recent_generations": [_generation_brief(item) for item in recent_generations],
        "links": {
            "canonical_url": preview.canonical_url,
            "anchor_pr_url": preview.anchor_pr_url,
        },
    }


def build_preview_inventory_payload(
    *,
    record_store: FilesystemRecordStore,
    context_name: str = "",
) -> dict[str, object]:
    previews = record_store.list_preview_records(context_name=context_name)
    preview_rows = []
    for preview in previews:
        generations = record_store.list_preview_generation_records(preview_id=preview.preview_id)
        generations_by_id = {record.generation_id: record for record in generations}
        serving_generation = generations_by_id.get(preview.serving_generation_id)
        latest_generation = generations_by_id.get(preview.latest_generation_id)
        input_generation = serving_generation or latest_generation
        evidence_generation = serving_generation or latest_generation
        preview_rows.append(
            {
                "preview_id": preview.preview_id,
                "context": preview.context,
                "anchor_repo": preview.anchor_repo,
                "anchor_pr_number": preview.anchor_pr_number,
                "preview_label": preview.preview_label,
                "canonical_url": preview.canonical_url,
                "state": preview.state,
                "updated_at": preview.updated_at,
                "destroy_after": preview.destroy_after,
                "destroyed_at": preview.destroyed_at,
                "destroy_reason": preview.destroy_reason,
                "serving_generation_id": preview.serving_generation_id,
                "latest_generation_id": preview.latest_generation_id,
                "artifact_id": evidence_generation.artifact_id if evidence_generation is not None else "",
                "manifest_fingerprint": (
                    input_generation.resolved_manifest_fingerprint if input_generation is not None else ""
                ),
                "overall_health_status": (
                    evidence_generation.overall_health_status if evidence_generation is not None else "pending"
                ),
                "status_summary": _status_summary(
                    preview=preview,
                    serving_generation=serving_generation,
                    latest_generation=latest_generation,
                ),
                "next_action": _next_action(
                    preview=preview,
                    serving_generation=serving_generation,
                    latest_generation=latest_generation,
                ),
            }
        )
    return {
        "context": context_name,
        "count": len(preview_rows),
        "previews": preview_rows,
    }


def build_preview_history_payload(
    *,
    record_store: FilesystemRecordStore,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> dict[str, object] | None:
    preview = find_preview_record(
        record_store=record_store,
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if preview is None:
        return None

    generations = record_store.list_preview_generation_records(preview_id=preview.preview_id)
    return {
        "preview": {
            "preview_id": preview.preview_id,
            "context": preview.context,
            "anchor_repo": preview.anchor_repo,
            "anchor_pr_number": preview.anchor_pr_number,
            "preview_label": preview.preview_label,
            "canonical_url": preview.canonical_url,
            "state": preview.state,
            "updated_at": preview.updated_at,
            "serving_generation_id": preview.serving_generation_id,
            "latest_generation_id": preview.latest_generation_id,
            "active_generation_id": preview.active_generation_id,
        },
        "generation_count": len(generations),
        "generations": [
            {
                **_generation_payload_required(record),
                "is_active": record.generation_id == preview.active_generation_id,
                "is_serving": record.generation_id == preview.serving_generation_id,
                "is_latest": record.generation_id == preview.latest_generation_id,
            }
            for record in generations
        ],
    }


def _generation_payload(record: PreviewGenerationRecord | None) -> dict[str, object] | None:
    if record is None:
        return None
    return {
        "generation_id": record.generation_id,
        "sequence": record.sequence,
        "state": record.state,
        "requested_reason": record.requested_reason,
        "requested_at": record.requested_at,
        "started_at": record.started_at,
        "ready_at": record.ready_at,
        "finished_at": record.finished_at,
        "failed_at": record.failed_at,
        "superseded_at": record.superseded_at,
        "expires_at": record.expires_at,
        "artifact_id": record.artifact_id,
        "resolved_manifest_fingerprint": record.resolved_manifest_fingerprint,
        "baseline_release_tuple_id": record.baseline_release_tuple_id,
        "deploy_status": record.deploy_status,
        "verify_status": record.verify_status,
        "overall_health_status": record.overall_health_status,
        "failure_stage": record.failure_stage,
        "failure_summary": record.failure_summary,
    }


def _generation_brief(record: PreviewGenerationRecord) -> dict[str, object]:
    return {
        "generation_id": record.generation_id,
        "sequence": record.sequence,
        "state": record.state,
        "artifact_id": record.artifact_id,
        "resolved_manifest_fingerprint": record.resolved_manifest_fingerprint,
        "requested_reason": record.requested_reason,
        "requested_at": record.requested_at,
        "ready_at": record.ready_at,
        "failed_at": record.failed_at,
        "failure_stage": record.failure_stage,
    }


def _generation_payload_required(record: PreviewGenerationRecord) -> dict[str, object]:
    return _generation_payload(record) or {}


def _next_preview_generation_sequence(
    *, existing_generations: tuple[PreviewGenerationRecord, ...]
) -> int:
    if not existing_generations:
        return 1
    return max(record.sequence for record in existing_generations) + 1


def _status_summary(
    *,
    preview: PreviewRecord,
    serving_generation: PreviewGenerationRecord | None,
    latest_generation: PreviewGenerationRecord | None,
) -> str:
    if preview.state == "destroyed":
        return "Preview destroyed; evidence retained."
    if preview.state == "paused":
        return "Preview is paused; no new generations will start until resumed."
    if latest_generation is None:
        return "Waiting for the first generation."
    if serving_generation is None:
        return "No serving preview is available yet."
    if serving_generation.generation_id == latest_generation.generation_id:
        return "Serving the latest requested generation."
    if latest_generation.state == "failed":
        return "Serving the last healthy generation while the latest replacement failed."
    return "Serving a prior generation while Harbor prepares a replacement."


def _next_action(
    *,
    preview: PreviewRecord,
    serving_generation: PreviewGenerationRecord | None,
    latest_generation: PreviewGenerationRecord | None,
) -> str:
    if preview.state == "destroyed":
        return "No runtime action remains; Harbor is retaining historical evidence only."
    if preview.state == "teardown_pending":
        return "Harbor will destroy runtime resources after the current teardown window."
    if preview.state == "paused":
        return "Harbor will keep current evidence but will not start new generations until resumed."
    if latest_generation is None:
        return "Harbor is waiting to create the first generation for this preview."
    if latest_generation.state in {"resolving", "building", "deploying", "verifying"}:
        return f"Harbor is progressing generation {latest_generation.generation_id} toward readiness."
    if latest_generation.state == "failed" and serving_generation is not None:
        return "Harbor is retaining the prior serving generation because the latest replacement failed."
    if preview.destroy_after:
        return "Harbor will keep this preview until the current destroy-after deadline or a lifecycle event replaces it."
    return "Harbor is serving the current preview state."
