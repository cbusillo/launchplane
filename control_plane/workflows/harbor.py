import re
import tomllib
from pathlib import Path

import click
from pydantic import ValidationError

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.github_pull_request_event import GitHubPullRequestEvent
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewGenerationState,
    PreviewPullRequestSummary,
    PreviewSourceRecord,
)
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
HarborPullRequestAction = str
HARBOR_TENANT_ANCHOR_CONTEXTS: dict[str, str] = {
    "tenant-cm": "cm",
    "tenant-opw": "opw",
}
HARBOR_PREVIEW_REQUEST_BLOCK_PATTERN = re.compile(
    rf"```{re.escape(HARBOR_PREVIEW_REQUEST_BLOCK_INFO_STRING)}[ \t]*\r?\n(?P<body>.*?)\r?\n```",
    flags=re.IGNORECASE | re.DOTALL,
)


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
    record_store: FilesystemRecordStore,
    event: GitHubPullRequestEvent,
) -> dict[str, object]:
    action, resolved_context, preview = resolve_pull_request_event_decision(
        record_store=record_store,
        event=event,
    )
    mutation_intent = build_pull_request_event_mutation_intent(
        event=event,
        action=action,
        resolved_context=resolved_context,
        preview=preview,
    )
    return {
        "event": event.model_dump(mode="json"),
        "decision": {
            "action": action,
            "anchor_repo_eligible": bool(resolved_context),
            "resolved_context": resolved_context,
            "label_enabled": harbor_preview_label_enabled(label_names=event.label_names),
            "preview_exists": preview is not None,
            "context_resolution_required": preview is None and not resolved_context,
        },
        "request_metadata": parse_preview_request_metadata(pr_body=event.pr_body).model_dump(mode="json"),
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
