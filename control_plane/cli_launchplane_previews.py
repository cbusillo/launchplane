import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from json import JSONDecodeError
from pathlib import Path

import click
from pydantic import ValidationError

from control_plane.contracts.github_pull_request_event import GitHubPullRequestEvent
from control_plane.contracts.github_webhook_replay_envelope import GitHubWebhookReplayEnvelope
from control_plane.contracts.preview_enablement_record import PreviewEnablementRecord
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_mutation_request import (
    LaunchplanePullRequestMutationIntent,
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.preview_manifest import LaunchplaneResolvedPreviewManifest
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_request_metadata import LaunchplanePreviewRequestParseResult
from control_plane.launchplane_mutations import (
    apply_launchplane_destroy_preview as shared_apply_launchplane_destroy_preview,
    apply_launchplane_generation_evidence as shared_apply_launchplane_generation_evidence,
    upsert_launchplane_preview_from_request as shared_upsert_launchplane_preview_from_request,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.launchplane import ProductProfileListStore
from control_plane.workflows.launchplane import (
    adapt_github_webhook_pull_request_event,
    apply_generation_failed_transition,
    apply_generation_ready_transition,
    apply_generation_requested_transition,
    build_pull_request_event_action_payload,
    build_pull_request_feedback_payload,
    build_preview_generation_record_from_request,
    build_preview_history_payload,
    build_preview_inventory_payload,
    build_preview_record_from_request,
    deliver_pull_request_feedback,
    find_preview_record,
    launchplane_anchor_repo_context,
    resolve_launchplane_github_webhook_secret,
    verify_github_webhook_signature,
)


_DATABASE_URL_ENV_KEYS = ("LAUNCHPLANE_DATABASE_URL",)
_DIRECT_DB_MUTATION_MESSAGE = (
    "Direct local DB mutation is restricted after the Launchplane service boundary. "
    "Use the deployed service route or operator workflow for shared/production changes, "
    "or pass --allow-direct-db-mutation only for explicit local/bootstrap repair."
)
LaunchplanePreviewRecordStore = FilesystemRecordStore | PostgresRecordStore


@dataclass(frozen=True)
class LaunchplanePreviewCliCallbacks:
    store_factory: Callable[..., LaunchplanePreviewRecordStore]
    control_plane_root: Callable[[], Path]
    load_json_file: Callable[[Path], dict[str, object]]
    require_preview_status_payload: Callable[..., dict[str, object]]
    build_tenant_payload: Callable[..., dict[str, object] | None]
    render_index_page_html: Callable[..., str]
    render_policy_page_html: Callable[..., str]
    write_site_bundle: Callable[..., None]
    render_status_page_html: Callable[[dict[str, object]], str]
    preview_profile_rows: Callable[[ProductProfileListStore], tuple[tuple[str, str], ...]]
    build_preview_enablement_record: Callable[..., PreviewEnablementRecord | None]
    load_github_webhook_json_bytes: Callable[..., dict[str, object]]
    json_object: Callable[[object], dict[str, object] | None]


_callbacks: LaunchplanePreviewCliCallbacks | None = None


def register_launchplane_preview_commands(
    main: click.Group, *, callbacks: LaunchplanePreviewCliCallbacks
) -> None:
    global _callbacks
    _callbacks = callbacks
    main.add_command(launchplane_previews)


def _preview_callbacks() -> LaunchplanePreviewCliCallbacks:
    if _callbacks is None:
        raise click.ClickException("Launchplane preview CLI callbacks are not configured.")
    return _callbacks


def _store(state_dir: Path, *, database_url: str | None = None) -> LaunchplanePreviewRecordStore:
    return _preview_callbacks().store_factory(state_dir, database_url=database_url)


def _resolve_preview_mutation_database_url(
    *,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
    command_label: str,
) -> str | None:
    if local_rehearsal:
        return None
    normalized_database_url = database_url.strip()
    if not normalized_database_url:
        for environment_key in _DATABASE_URL_ENV_KEYS:
            environment_value = os.environ.get(environment_key, "").strip()
            if environment_value:
                normalized_database_url = environment_value
                break
    if not normalized_database_url:
        raise click.ClickException(
            f"{command_label} requires --database-url or LAUNCHPLANE_DATABASE_URL. "
            "Use --local-rehearsal for explicit local filesystem rehearsal."
        )
    if not allow_direct_db_mutation:
        raise click.ClickException(_DIRECT_DB_MUTATION_MESSAGE)
    return normalized_database_url


def _direct_db_mutation_acknowledgement_option(function: Callable[..., object]) -> Callable[..., object]:
    return click.option(
        "--allow-direct-db-mutation",
        is_flag=True,
        default=False,
        help="Acknowledge direct local DB mutation for explicit local/bootstrap repair.",
    )(function)


def _control_plane_root() -> Path:
    return _preview_callbacks().control_plane_root()


def _load_json_file(input_file: Path) -> dict[str, object]:
    return _preview_callbacks().load_json_file(input_file)


def _require_launchplane_preview_status_payload(
    *, state_dir: Path, context_name: str, anchor_repo: str, anchor_pr_number: int
) -> dict[str, object]:
    return _preview_callbacks().require_preview_status_payload(
        state_dir=state_dir,
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )


def _build_launchplane_tenant_payload(
    *,
    control_plane_root: Path,
    record_store: LaunchplanePreviewRecordStore,
    context_name: str,
    anchor_repo: str = "",
) -> dict[str, object] | None:
    return _preview_callbacks().build_tenant_payload(
        control_plane_root=control_plane_root,
        record_store=record_store,
        context_name=context_name,
        anchor_repo=anchor_repo,
    )


def _render_launchplane_preview_index_page_html(
    payload: dict[str, object], *, tenant_payload: dict[str, object] | None
) -> str:
    return _preview_callbacks().render_index_page_html(payload, tenant_payload=tenant_payload)


def _render_launchplane_preview_policy_page_html(
    payload: dict[str, object], *, eligible_contexts: tuple[tuple[str, str], ...]
) -> str:
    return _preview_callbacks().render_policy_page_html(
        payload, eligible_contexts=eligible_contexts
    )


def _write_launchplane_site_bundle(*, state_dir: Path, output_dir: Path, context_name: str) -> None:
    _preview_callbacks().write_site_bundle(
        state_dir=state_dir, output_dir=output_dir, context_name=context_name
    )


def _render_launchplane_preview_status_page_html(payload: dict[str, object]) -> str:
    return _preview_callbacks().render_status_page_html(payload)


def _launchplane_preview_profile_rows(
    record_store: LaunchplanePreviewRecordStore,
) -> tuple[tuple[str, str], ...]:
    return _preview_callbacks().preview_profile_rows(record_store)


def _build_launchplane_preview_enablement_record(
    *,
    context_name: str,
    event: GitHubPullRequestEvent,
    request_metadata: LaunchplanePreviewRequestParseResult,
    resolved_manifest: LaunchplaneResolvedPreviewManifest | None = None,
) -> PreviewEnablementRecord | None:
    return _preview_callbacks().build_preview_enablement_record(
        context_name=context_name,
        event=event,
        request_metadata=request_metadata,
        resolved_manifest=resolved_manifest,
    )


def _load_github_webhook_json_bytes(
    raw_payload_bytes: bytes, *, description: str
) -> dict[str, object]:
    return _preview_callbacks().load_github_webhook_json_bytes(
        raw_payload_bytes, description=description
    )


def _json_object(value: object) -> dict[str, object] | None:
    return _preview_callbacks().json_object(value)


def _record_locator(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    normalized_value = str(value).strip()
    return normalized_value or fallback


def _generation_transition_payload(
    *,
    generation: PreviewGenerationRecord,
    preview: PreviewRecord,
    generation_locator: object,
    preview_locator: object,
) -> dict[str, str]:
    return {
        "generation_id": generation.generation_id,
        "generation_path": _record_locator(generation_locator, generation.generation_id),
        "preview_id": preview.preview_id,
        "preview_path": _record_locator(preview_locator, preview.preview_id),
    }


@click.group("launchplane-previews")
def launchplane_previews() -> None:
    """Launchplane preview record and read-model commands."""


@launchplane_previews.command("write-preview")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def launchplane_previews_write_preview(
    state_dir: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
    input_file: Path,
) -> None:
    execution_database_url = _resolve_preview_mutation_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
        command_label="launchplane-previews write-preview",
    )
    record_store = _store(state_dir, database_url=execution_database_url)
    request = PreviewMutationRequest.model_validate(_load_json_file(input_file))
    record = build_preview_record_from_request(
        control_plane_root=_control_plane_root(),
        record_store=record_store,
        request=request,
    )
    record_path = record_store.write_preview_record(record)
    click.echo(_record_locator(record_path, record.preview_id))


@launchplane_previews.command("write-generation")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def launchplane_previews_write_generation(
    state_dir: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
    input_file: Path,
) -> None:
    execution_database_url = _resolve_preview_mutation_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
        command_label="launchplane-previews write-generation",
    )
    record_store = _store(state_dir, database_url=execution_database_url)
    request = PreviewGenerationMutationRequest.model_validate(_load_json_file(input_file))
    record = build_preview_generation_record_from_request(
        record_store=record_store,
        request=request,
    )
    record_path = record_store.write_preview_generation_record(record)
    click.echo(_record_locator(record_path, record.generation_id))


@launchplane_previews.command("write-enablement")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def launchplane_previews_write_enablement(
    state_dir: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
    input_file: Path,
) -> None:
    execution_database_url = _resolve_preview_mutation_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
        command_label="launchplane-previews write-enablement",
    )
    record_store = _store(state_dir, database_url=execution_database_url)
    record = PreviewEnablementRecord.model_validate(_load_json_file(input_file))
    record_path = record_store.write_preview_enablement_record(record)
    click.echo(_record_locator(record_path, record.record_id))


@launchplane_previews.command("request-generation")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--preview-input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option(
    "--generation-input-file", type=click.Path(exists=True, path_type=Path), required=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
def launchplane_previews_request_generation(
    state_dir: Path,
    preview_input_file: Path,
    generation_input_file: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
) -> None:
    execution_database_url = _resolve_preview_mutation_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
        command_label="launchplane-previews request-generation",
    )
    record_store = _store(state_dir, database_url=execution_database_url)
    preview_request = PreviewMutationRequest.model_validate(_load_json_file(preview_input_file))
    generation_request = PreviewGenerationMutationRequest.model_validate(
        _load_json_file(generation_input_file)
    )
    result_payload = _apply_launchplane_request_generation(
        control_plane_root=_control_plane_root(),
        record_store=record_store,
        preview_request=preview_request,
        generation_request=generation_request,
    )
    click.echo(json.dumps(result_payload, indent=2, sort_keys=True))


@launchplane_previews.command("write-from-generation")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--preview-input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option(
    "--generation-input-file", type=click.Path(exists=True, path_type=Path), required=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
def launchplane_previews_write_from_generation(
    state_dir: Path,
    preview_input_file: Path,
    generation_input_file: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
) -> None:
    execution_database_url = _resolve_preview_mutation_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
        command_label="launchplane-previews write-from-generation",
    )
    record_store = _store(state_dir, database_url=execution_database_url)
    preview_request = PreviewMutationRequest.model_validate(_load_json_file(preview_input_file))
    generation_request = PreviewGenerationMutationRequest.model_validate(
        _load_json_file(generation_input_file)
    )
    result_payload = _apply_launchplane_generation_evidence(
        control_plane_root=_control_plane_root(),
        record_store=record_store,
        preview_request=preview_request,
        generation_request=generation_request,
    )
    click.echo(json.dumps(result_payload, indent=2, sort_keys=True))


@launchplane_previews.command("write-destroyed")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def launchplane_previews_write_destroyed(
    state_dir: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
    input_file: Path,
) -> None:
    execution_database_url = _resolve_preview_mutation_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
        command_label="launchplane-previews write-destroyed",
    )
    record_store = _store(state_dir, database_url=execution_database_url)
    request = PreviewDestroyMutationRequest.model_validate(_load_json_file(input_file))
    result_payload = _apply_launchplane_destroy_preview(
        record_store=record_store,
        request=request,
    )
    click.echo(json.dumps(result_payload, indent=2, sort_keys=True))


@launchplane_previews.command("mark-generation-ready")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def launchplane_previews_mark_generation_ready(
    state_dir: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
    input_file: Path,
) -> None:
    execution_database_url = _resolve_preview_mutation_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
        command_label="launchplane-previews mark-generation-ready",
    )
    record_store = _store(state_dir, database_url=execution_database_url)
    request = PreviewGenerationMutationRequest.model_validate(_load_json_file(input_file))
    if not request.generation_id.strip():
        raise click.ClickException("Ready-generation transition requires generation_id.")
    preview_record = _read_launchplane_preview_or_fail(
        record_store=record_store,
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
    )
    _read_launchplane_generation_or_fail(
        record_store=record_store,
        preview_id=preview_record.preview_id,
        generation_id=request.generation_id,
    )
    generation_record = build_preview_generation_record_from_request(
        record_store=record_store,
        request=request,
    )
    transitioned_preview = apply_generation_ready_transition(
        preview=preview_record,
        generation=generation_record,
    )
    generation_path = record_store.write_preview_generation_record(generation_record)
    preview_path = record_store.write_preview_record(transitioned_preview)
    click.echo(
        json.dumps(
            _generation_transition_payload(
                generation=generation_record,
                preview=transitioned_preview,
                generation_locator=generation_path,
                preview_locator=preview_path,
            ),
            indent=2,
            sort_keys=True,
        )
    )


@launchplane_previews.command("mark-generation-failed")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def launchplane_previews_mark_generation_failed(
    state_dir: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
    input_file: Path,
) -> None:
    execution_database_url = _resolve_preview_mutation_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
        command_label="launchplane-previews mark-generation-failed",
    )
    record_store = _store(state_dir, database_url=execution_database_url)
    request = PreviewGenerationMutationRequest.model_validate(_load_json_file(input_file))
    if not request.generation_id.strip():
        raise click.ClickException("Failed-generation transition requires generation_id.")
    preview_record = _read_launchplane_preview_or_fail(
        record_store=record_store,
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
    )
    _read_launchplane_generation_or_fail(
        record_store=record_store,
        preview_id=preview_record.preview_id,
        generation_id=request.generation_id,
    )
    generation_record = build_preview_generation_record_from_request(
        record_store=record_store,
        request=request,
    )
    transitioned_preview = apply_generation_failed_transition(
        preview=preview_record,
        generation=generation_record,
    )
    generation_path = record_store.write_preview_generation_record(generation_record)
    preview_path = record_store.write_preview_record(transitioned_preview)
    click.echo(
        json.dumps(
            _generation_transition_payload(
                generation=generation_record,
                preview=transitioned_preview,
                generation_locator=generation_path,
                preview_locator=preview_path,
            ),
            indent=2,
            sort_keys=True,
        )
    )


@launchplane_previews.command("destroy-preview")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def launchplane_previews_destroy_preview(
    state_dir: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
    input_file: Path,
) -> None:
    execution_database_url = _resolve_preview_mutation_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
        command_label="launchplane-previews destroy-preview",
    )
    record_store = _store(state_dir, database_url=execution_database_url)
    request = PreviewDestroyMutationRequest.model_validate(_load_json_file(input_file))
    result_payload = _apply_launchplane_destroy_preview(
        record_store=record_store,
        request=request,
    )
    click.echo(result_payload["preview_path"])


@launchplane_previews.command("ingest-pr-event")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--apply", "apply_intent", is_flag=True)
@click.option("--deliver-feedback", is_flag=True)
def launchplane_previews_ingest_pr_event(
    state_dir: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
    input_file: Path,
    apply_intent: bool,
    deliver_feedback: bool,
) -> None:
    execution_database_url = _resolve_preview_mutation_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
        command_label="launchplane-previews ingest-pr-event",
    )
    event = GitHubPullRequestEvent.model_validate(_load_json_file(input_file))
    payload = _ingest_launchplane_pr_event_payload(
        state_dir=state_dir,
        database_url=execution_database_url,
        event=event,
        apply_intent=apply_intent,
        deliver_feedback=deliver_feedback,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@launchplane_previews.command("ingest-github-webhook")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--event-name", default="pull_request", show_default=True)
@click.option("--delivery-id", default="", help="Optional GitHub delivery id for traceability.")
@click.option("--signature-256", default="", help="Raw X-Hub-Signature-256 header value.")
@click.option(
    "--allow-unsigned",
    is_flag=True,
    help="Explicit local/manual bypass for signature verification.",
)
@click.option("--apply", "apply_intent", is_flag=True)
@click.option("--deliver-feedback", is_flag=True)
def launchplane_previews_ingest_github_webhook(
    state_dir: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
    input_file: Path,
    event_name: str,
    delivery_id: str,
    signature_256: str,
    allow_unsigned: bool,
    apply_intent: bool,
    deliver_feedback: bool,
) -> None:
    execution_database_url = _resolve_preview_mutation_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
        command_label="launchplane-previews ingest-github-webhook",
    )
    raw_payload_bytes, webhook_payload = _load_github_webhook_json_file(input_file)
    payload = _ingest_launchplane_github_webhook_payload(
        state_dir=state_dir,
        database_url=execution_database_url,
        event_name=event_name,
        raw_payload_bytes=raw_payload_bytes,
        webhook_payload=webhook_payload,
        delivery_id=delivery_id,
        delivery_source="github-webhook",
        signature_256=signature_256,
        allow_unsigned=allow_unsigned,
        apply_intent=apply_intent,
        deliver_feedback=deliver_feedback,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _load_json_object_file(input_file: Path, *, description: str) -> dict[str, object]:
    try:
        payload = json.loads(input_file.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, JSONDecodeError) as exc:
        raise click.ClickException(f"{description} must be valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{description} must decode to a JSON object.")
    return payload


def _load_github_webhook_capture_headers(input_file: Path) -> dict[str, str]:
    payload = _load_json_object_file(input_file, description="GitHub webhook headers file")
    headers: dict[str, str] = {}
    for header_name, header_value in payload.items():
        if not isinstance(header_value, str):
            raise click.ClickException(
                "GitHub webhook headers file must map header names to string values."
            )
        headers[str(header_name)] = header_value
    return headers


def _github_webhook_capture_header_value(headers: dict[str, str], name: str) -> str:
    normalized_name = name.strip().lower()
    if not normalized_name:
        return ""
    for header_name, header_value in headers.items():
        if header_name.strip().lower() == normalized_name:
            return header_value.strip()
    return ""


def _github_webhook_capture_metadata_headers(headers: dict[str, str]) -> dict[str, str]:
    exact_redacted_header_names = {
        "authorization",
        "baggage",
        "cookie",
        "cf-connecting-ip",
        "proxy-authorization",
        "true-client-ip",
        "x-real-ip",
    }
    metadata_headers: dict[str, str] = {}
    for header_name, header_value in headers.items():
        normalized_header_name = header_name.strip().lower()
        if (
            normalized_header_name in exact_redacted_header_names
            or normalized_header_name == "forwarded"
        ):
            metadata_headers[header_name] = "[redacted]"
            continue
        if normalized_header_name.startswith("x-forwarded-"):
            metadata_headers[header_name] = "[redacted]"
            continue
        metadata_headers[header_name] = header_value
    return metadata_headers


def _split_http_capture_text(http_capture_text: str) -> tuple[str, str]:
    for delimiter in ("\r\n\r\n", "\n\n"):
        if delimiter in http_capture_text:
            header_text, body_text = http_capture_text.split(delimiter, 1)
            return header_text, body_text
    raise click.ClickException(
        "GitHub webhook HTTP capture must contain headers, a blank line, and a JSON body."
    )


def _parse_github_webhook_http_capture(input_file: Path) -> tuple[str, dict[str, str], bytes]:
    try:
        http_capture_text = input_file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise click.ClickException(
            f"GitHub webhook HTTP capture must be valid UTF-8 text: {exc}"
        ) from exc
    header_text, body_text = _split_http_capture_text(http_capture_text)
    header_lines = header_text.splitlines()
    if not header_lines:
        raise click.ClickException("GitHub webhook HTTP capture is missing the request line.")
    request_line = header_lines[0].strip()
    if not request_line.startswith("POST ") or "HTTP/" not in request_line:
        raise click.ClickException(
            "GitHub webhook HTTP capture must start with a POST request line such as 'POST /github-webhook HTTP/1.1'."
        )
    headers: dict[str, str] = {}
    for header_line in header_lines[1:]:
        if not header_line.strip():
            continue
        if ":" not in header_line:
            raise click.ClickException(
                "GitHub webhook HTTP capture headers must use the 'Name: value' format."
            )
        header_name, header_value = header_line.split(":", 1)
        normalized_name = header_name.strip()
        if not normalized_name:
            raise click.ClickException("GitHub webhook HTTP capture contains a blank header name.")
        headers[normalized_name] = header_value.strip()
    if not headers:
        raise click.ClickException("GitHub webhook HTTP capture must include at least one header.")
    for override_header_name in ("X-HTTP-Method-Override", "X-Method-Override"):
        override_method = _github_webhook_capture_header_value(headers, override_header_name)
        if override_method and override_method.upper() != "POST":
            raise click.ClickException(
                f"GitHub webhook HTTP capture {override_header_name} must not conflict with the captured POST request."
            )
    declared_transfer_encoding = _github_webhook_capture_header_value(headers, "Transfer-Encoding")
    if declared_transfer_encoding and declared_transfer_encoding.lower() != "identity":
        raise click.ClickException(
            "GitHub webhook HTTP capture Transfer-Encoding is unsupported for the saved-capture replay path."
        )
    declared_content_encoding = _github_webhook_capture_header_value(headers, "Content-Encoding")
    if declared_content_encoding and declared_content_encoding.lower() != "identity":
        raise click.ClickException(
            "GitHub webhook HTTP capture Content-Encoding is unsupported for the saved-capture replay path."
        )
    declared_trailer = _github_webhook_capture_header_value(headers, "Trailer")
    if declared_trailer:
        raise click.ClickException(
            "GitHub webhook HTTP capture Trailer declarations are unsupported for the saved-capture replay path."
        )
    declared_expect = _github_webhook_capture_header_value(headers, "Expect")
    if declared_expect:
        raise click.ClickException(
            "GitHub webhook HTTP capture Expect declarations are unsupported for the saved-capture replay path."
        )
    declared_connection = _github_webhook_capture_header_value(headers, "Connection")
    if declared_connection:
        raise click.ClickException(
            "GitHub webhook HTTP capture Connection declarations are unsupported for the saved-capture replay path."
        )
    declared_pragma = _github_webhook_capture_header_value(headers, "Pragma")
    if declared_pragma:
        raise click.ClickException(
            "GitHub webhook HTTP capture Pragma declarations are unsupported for the saved-capture replay path."
        )
    declared_cache_control = _github_webhook_capture_header_value(headers, "Cache-Control")
    if declared_cache_control:
        raise click.ClickException(
            "GitHub webhook HTTP capture Cache-Control declarations are unsupported for the saved-capture replay path."
        )
    declared_upgrade = _github_webhook_capture_header_value(headers, "Upgrade")
    if declared_upgrade:
        raise click.ClickException(
            "GitHub webhook HTTP capture Upgrade declarations are unsupported for the saved-capture replay path."
        )
    declared_te = _github_webhook_capture_header_value(headers, "TE")
    if declared_te:
        raise click.ClickException(
            "GitHub webhook HTTP capture TE declarations are unsupported for the saved-capture replay path."
        )
    declared_keep_alive = _github_webhook_capture_header_value(headers, "Keep-Alive")
    if declared_keep_alive:
        raise click.ClickException(
            "GitHub webhook HTTP capture Keep-Alive declarations are unsupported for the saved-capture replay path."
        )
    declared_proxy_connection = _github_webhook_capture_header_value(headers, "Proxy-Connection")
    if declared_proxy_connection:
        raise click.ClickException(
            "GitHub webhook HTTP capture Proxy-Connection declarations are unsupported for the saved-capture replay path."
        )
    declared_content_type = _github_webhook_capture_header_value(headers, "Content-Type")
    if declared_content_type:
        normalized_media_type = declared_content_type.split(";", 1)[0].strip().lower()
        if normalized_media_type not in {"application/json"} and not normalized_media_type.endswith(
            "+json"
        ):
            raise click.ClickException(
                "GitHub webhook HTTP capture Content-Type must be JSON when present."
            )
    body_bytes = body_text.encode("utf-8")
    declared_content_length = _github_webhook_capture_header_value(headers, "Content-Length")
    if declared_content_length:
        try:
            expected_content_length = int(declared_content_length)
        except ValueError as exc:
            raise click.ClickException(
                "GitHub webhook HTTP capture Content-Length header must be an integer when present."
            ) from exc
        if expected_content_length < 0:
            raise click.ClickException(
                "GitHub webhook HTTP capture Content-Length header must not be negative."
            )
        if expected_content_length != len(body_bytes):
            raise click.ClickException(
                "GitHub webhook HTTP capture Content-Length does not match the saved body bytes."
            )
    return request_line, headers, body_bytes


def _merge_github_webhook_capture_evidence(
    *,
    evidence_payload: dict[str, object] | None,
    request_line: str,
) -> dict[str, object] | None:
    if not request_line.strip():
        return evidence_payload

    merged_evidence = dict(evidence_payload) if evidence_payload is not None else {}
    http_request_payload = merged_evidence.get("http_request")
    if http_request_payload is None:
        merged_evidence["http_request"] = {"request_line": request_line}
        return merged_evidence
    if not isinstance(http_request_payload, dict):
        raise click.ClickException(
            "GitHub webhook evidence file field 'http_request' must be a JSON object when provided."
        )

    request_line_payload = http_request_payload.get("request_line")
    if request_line_payload is not None:
        if not isinstance(request_line_payload, str):
            raise click.ClickException(
                "GitHub webhook evidence file field 'http_request.request_line' must be a string when provided."
            )
        if request_line_payload != request_line:
            raise click.ClickException(
                "GitHub webhook evidence file request_line conflicts with the saved HTTP capture request line."
            )
        return merged_evidence

    merged_http_request_payload = dict(http_request_payload)
    merged_http_request_payload["request_line"] = request_line
    merged_evidence["http_request"] = merged_http_request_payload
    return merged_evidence


@launchplane_previews.command("build-github-webhook-replay-envelope")
@click.option("--payload-file", type=click.Path(exists=True, path_type=Path))
@click.option("--http-capture-file", type=click.Path(exists=True, path_type=Path))
@click.option("--headers-file", type=click.Path(exists=True, path_type=Path))
@click.option("--event-name", default="", help="Optional GitHub event name override.")
@click.option("--signature-256", default="", help="Optional X-Hub-Signature-256 override.")
@click.option("--delivery-id", default="", help="Optional GitHub delivery id override.")
@click.option("--delivery-source", default="", help="Optional top-level delivery source override.")
@click.option("--allow-unsigned", is_flag=True, help="Emit an explicit unsigned replay envelope.")
@click.option("--recorded-at", default="", help="Optional capture timestamp for traceability.")
@click.option(
    "--capture-source", default="", help="Optional capture source label for replay metadata."
)
@click.option("--evidence-file", type=click.Path(exists=True, path_type=Path))
@click.option("--output-file", type=click.Path(path_type=Path))
def launchplane_previews_build_github_webhook_replay_envelope(
    payload_file: Path | None,
    http_capture_file: Path | None,
    headers_file: Path | None,
    event_name: str,
    signature_256: str,
    delivery_id: str,
    delivery_source: str,
    allow_unsigned: bool,
    recorded_at: str,
    capture_source: str,
    evidence_file: Path | None,
    output_file: Path | None,
) -> None:
    if (payload_file is None) == (http_capture_file is None):
        raise click.ClickException(
            "Provide exactly one of --payload-file or --http-capture-file when building a GitHub replay envelope."
        )
    if http_capture_file is not None and headers_file is not None:
        raise click.ClickException(
            "--headers-file cannot be combined with --http-capture-file because the HTTP capture already carries headers."
        )

    capture_headers: dict[str, str] = {}
    captured_request_line = ""
    if http_capture_file is not None:
        captured_request_line, capture_headers, raw_payload_bytes = (
            _parse_github_webhook_http_capture(http_capture_file)
        )
        _load_github_webhook_json_bytes(
            raw_payload_bytes, description="GitHub webhook HTTP capture body"
        )
    else:
        assert payload_file is not None
        raw_payload_bytes, _ = _load_github_webhook_json_file(payload_file)
        capture_headers = (
            _load_github_webhook_capture_headers(headers_file) if headers_file is not None else {}
        )
    capture_event_name = _github_webhook_capture_header_value(capture_headers, "X-GitHub-Event")
    evidence_payload = (
        _load_json_object_file(evidence_file, description="GitHub webhook evidence file")
        if evidence_file is not None
        else None
    )
    evidence_payload = _merge_github_webhook_capture_evidence(
        evidence_payload=evidence_payload,
        request_line=captured_request_line,
    )
    raw_payload_text = raw_payload_bytes.decode("utf-8")

    capture_payload: dict[str, object] | None = None
    if (
        capture_headers
        or recorded_at.strip()
        or capture_source.strip()
        or evidence_payload is not None
    ):
        capture_payload = {}
        if recorded_at.strip():
            capture_payload["recorded_at"] = recorded_at.strip()
        if capture_source.strip():
            capture_payload["source"] = capture_source.strip()
        capture_metadata_headers = _github_webhook_capture_metadata_headers(capture_headers)
        if capture_metadata_headers:
            capture_payload["headers"] = capture_metadata_headers
        if evidence_payload is not None:
            capture_payload["evidence"] = evidence_payload

    resolved_event_name = event_name.strip() or capture_event_name or "pull_request"
    top_level_event_name = event_name.strip() or ("" if capture_event_name else resolved_event_name)
    top_level_signature_256 = signature_256.strip()
    top_level_delivery_id = delivery_id.strip()
    envelope = GitHubWebhookReplayEnvelope.model_validate(
        {
            "schema_version": 1,
            "adapter": "github_webhook",
            "event_name": top_level_event_name,
            "signature_256": top_level_signature_256,
            "allow_unsigned": allow_unsigned,
            "delivery_id": top_level_delivery_id,
            "delivery_source": delivery_source.strip(),
            "payload_text": raw_payload_text,
            "capture": capture_payload,
        }
    )
    envelope_json = json.dumps(
        envelope.model_dump(mode="json", exclude_none=True),
        indent=2,
        sort_keys=True,
    )
    if output_file is not None:
        output_file.write_text(f"{envelope_json}\n", encoding="utf-8")
    click.echo(envelope_json)


@launchplane_previews.command("replay-github-webhook")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--apply", "apply_intent", is_flag=True)
@click.option("--deliver-feedback", is_flag=True)
def launchplane_previews_replay_github_webhook(
    state_dir: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
    input_file: Path,
    apply_intent: bool,
    deliver_feedback: bool,
) -> None:
    execution_database_url = _resolve_preview_mutation_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
        command_label="launchplane-previews replay-github-webhook",
    )
    try:
        envelope = GitHubWebhookReplayEnvelope.model_validate(_load_json_file(input_file))
    except ValidationError as exc:
        raise click.ClickException(f"Invalid GitHub webhook replay envelope: {exc}") from exc
    resolved_event_name = envelope.resolved_event_name()
    resolved_delivery_id = envelope.resolved_delivery_id()
    resolved_delivery_source = envelope.resolved_delivery_source()
    raw_payload_bytes, webhook_payload = _load_github_webhook_replay_envelope(envelope)
    payload = _ingest_launchplane_github_webhook_payload(
        state_dir=state_dir,
        database_url=execution_database_url,
        event_name=resolved_event_name,
        raw_payload_bytes=raw_payload_bytes,
        webhook_payload=webhook_payload,
        delivery_id=resolved_delivery_id,
        delivery_source=resolved_delivery_source,
        signature_256=envelope.resolved_signature_256(),
        allow_unsigned=envelope.allow_unsigned,
        apply_intent=apply_intent,
        deliver_feedback=deliver_feedback,
    )
    webhook_replay_payload: dict[str, object] = {
        "adapter": envelope.adapter,
        "event_name": resolved_event_name,
        "delivery_id": resolved_delivery_id,
        "delivery_source": resolved_delivery_source,
    }
    replay_capture_payload = envelope.replay_capture_payload()
    if replay_capture_payload is not None:
        webhook_replay_payload["capture"] = replay_capture_payload
    payload["webhook_replay"] = webhook_replay_payload
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _ingest_launchplane_pr_event_payload(
    *,
    state_dir: Path,
    database_url: str | None,
    event: GitHubPullRequestEvent,
    apply_intent: bool,
    deliver_feedback: bool,
) -> dict[str, object]:
    control_plane_root = _control_plane_root()
    record_store = _store(state_dir, database_url=database_url)
    payload = build_pull_request_event_action_payload(
        control_plane_root=control_plane_root,
        record_store=record_store,
        event=event,
    )
    decision_payload = payload.get("decision")
    resolved_context = (
        str(decision_payload.get("resolved_context", "")).strip()
        if isinstance(decision_payload, dict)
        else ""
    )
    preview_payload = payload.get("preview")
    if not resolved_context and isinstance(preview_payload, dict):
        resolved_context = str(preview_payload.get("context", "")).strip()
    request_metadata_payload = payload.get("request_metadata")
    request_metadata = (
        LaunchplanePreviewRequestParseResult.model_validate(request_metadata_payload)
        if isinstance(request_metadata_payload, dict)
        else LaunchplanePreviewRequestParseResult(status="missing")
    )
    resolved_manifest_payload = payload.get("manifest")
    resolved_manifest = (
        LaunchplaneResolvedPreviewManifest.model_validate(resolved_manifest_payload)
        if isinstance(resolved_manifest_payload, dict)
        else None
    )
    enablement_record = _build_launchplane_preview_enablement_record(
        context_name=resolved_context,
        event=event,
        request_metadata=request_metadata,
        resolved_manifest=resolved_manifest,
    )
    if enablement_record is not None:
        record_store.write_preview_enablement_record(enablement_record)
    payload["enablement_record"] = (
        enablement_record.model_dump(mode="json") if enablement_record is not None else None
    )
    if apply_intent:
        payload["apply"] = _apply_launchplane_pr_event_intent(
            control_plane_root=control_plane_root,
            record_store=record_store,
            payload=payload,
        )
        request_metadata_payload = payload.get("request_metadata")
        action = decision_payload.get("action", "") if isinstance(decision_payload, dict) else ""
        apply_result = _json_object(payload.get("apply"))
        payload["feedback"] = build_pull_request_feedback_payload(
            record_store=record_store,
            event=event,
            action=action if isinstance(action, str) else "",
            preview=None,
            request_metadata=LaunchplanePreviewRequestParseResult.model_validate(
                request_metadata_payload
            ),
            resolved_manifest=(
                LaunchplaneResolvedPreviewManifest.model_validate(payload["manifest"])
                if isinstance(payload.get("manifest"), dict)
                else None
            ),
            apply_result=apply_result,
        )
    if deliver_feedback:
        resolved_context = (
            decision_payload.get("resolved_context", "")
            if isinstance(decision_payload, dict)
            else ""
        )
        feedback_payload = payload.get("feedback")
        if not isinstance(feedback_payload, dict):
            raise click.ClickException("Launchplane feedback payload is missing before delivery.")
        payload["feedback_delivery"] = deliver_pull_request_feedback(
            control_plane_root=control_plane_root,
            record_store=record_store,
            event=event,
            resolved_context=resolved_context if isinstance(resolved_context, str) else "",
            feedback_payload=feedback_payload,
        )
    return payload


def _ingest_launchplane_github_webhook_payload(
    *,
    state_dir: Path,
    database_url: str | None,
    event_name: str,
    raw_payload_bytes: bytes,
    webhook_payload: dict[str, object],
    delivery_id: str,
    delivery_source: str,
    signature_256: str,
    allow_unsigned: bool,
    apply_intent: bool,
    deliver_feedback: bool,
) -> dict[str, object]:
    control_plane_root = _control_plane_root()
    record_store = _store(state_dir, database_url=database_url)
    signature_verification = _verify_launchplane_github_webhook_signature(
        record_store=record_store,
        control_plane_root=control_plane_root,
        event_name=event_name,
        webhook_payload=webhook_payload,
        raw_payload_bytes=raw_payload_bytes,
        signature_256=signature_256,
        allow_unsigned=allow_unsigned,
    )
    event = adapt_github_webhook_pull_request_event(
        event_name=event_name,
        webhook_payload=webhook_payload,
    )
    payload = _ingest_launchplane_pr_event_payload(
        state_dir=state_dir,
        database_url=database_url,
        event=event,
        apply_intent=apply_intent,
        deliver_feedback=deliver_feedback,
    )
    payload["webhook"] = {
        "event_name": event_name,
        "adapter": "github_pull_request",
        "delivery": {
            "delivery_id": delivery_id.strip(),
            "delivery_source": delivery_source.strip() or "github-webhook",
        },
        "signature_verification": signature_verification,
    }
    return payload


def _load_github_webhook_json_file(input_file: Path) -> tuple[bytes, dict[str, object]]:
    raw_payload_bytes = input_file.read_bytes()
    webhook_payload = _load_github_webhook_json_bytes(
        raw_payload_bytes,
        description="GitHub webhook input file",
    )
    return raw_payload_bytes, webhook_payload


def _load_github_webhook_replay_envelope(
    envelope: GitHubWebhookReplayEnvelope,
) -> tuple[bytes, dict[str, object]]:
    if envelope.payload_text.strip():
        raw_payload_bytes = envelope.payload_text.encode("utf-8")
        try:
            webhook_payload = json.loads(envelope.payload_text)
        except JSONDecodeError as exc:
            raise click.ClickException(
                f"GitHub webhook replay envelope payload_text must be valid JSON: {exc}"
            ) from exc
        if not isinstance(webhook_payload, dict):
            raise click.ClickException(
                "GitHub webhook replay envelope payload_text must decode to a JSON object."
            )
        return raw_payload_bytes, webhook_payload
    if envelope.payload is None:
        raise click.ClickException("GitHub webhook replay envelope is missing payload content.")
    return json.dumps(envelope.payload).encode("utf-8"), envelope.payload


def _verify_launchplane_github_webhook_signature(
    *,
    record_store: LaunchplanePreviewRecordStore,
    control_plane_root: Path,
    event_name: str,
    webhook_payload: dict[str, object],
    raw_payload_bytes: bytes,
    signature_256: str,
    allow_unsigned: bool,
) -> dict[str, object]:
    if allow_unsigned:
        return {
            "mode": "bypass",
            "verified": False,
            "reason": "allow_unsigned",
        }

    context_name = _resolve_launchplane_github_webhook_context(
        record_store=record_store,
        event_name=event_name,
        webhook_payload=webhook_payload,
    )
    if not context_name:
        raise click.ClickException(
            "GitHub webhook signature verification could not resolve a Launchplane context from the raw payload."
        )
    secret = resolve_launchplane_github_webhook_secret(
        control_plane_root=control_plane_root,
        context_name=context_name,
    )
    if not secret:
        raise click.ClickException(
            f"Runtime environments file is missing GITHUB_WEBHOOK_SECRET for Launchplane context {context_name!r}."
        )
    verify_github_webhook_signature(
        payload_bytes=raw_payload_bytes,
        signature_header=signature_256,
        secret=secret,
    )
    return {
        "mode": "verified",
        "verified": True,
        "context": context_name,
    }


def _resolve_launchplane_github_webhook_context(
    *,
    record_store: LaunchplanePreviewRecordStore,
    event_name: str,
    webhook_payload: dict[str, object],
) -> str:
    if event_name.strip() != "pull_request":
        return ""
    repository_payload = webhook_payload.get("repository")
    if not isinstance(repository_payload, dict):
        return ""
    repo_name = repository_payload.get("name")
    if not isinstance(repo_name, str) or not repo_name.strip():
        return ""
    return launchplane_anchor_repo_context(record_store=record_store, repo=repo_name)


@launchplane_previews.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
def launchplane_previews_list(state_dir: Path, context_name: str) -> None:
    payload = build_preview_inventory_payload(
        record_store=_store(state_dir),
        context_name=context_name,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@launchplane_previews.command("show-tenant")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--anchor-repo", default="")
def launchplane_previews_show_tenant(
    state_dir: Path,
    context_name: str,
    anchor_repo: str,
) -> None:
    payload = _build_launchplane_tenant_payload(
        control_plane_root=_control_plane_root(),
        record_store=_store(state_dir),
        context_name=context_name,
        anchor_repo=anchor_repo,
    )
    if payload is None:
        raise click.ClickException(
            "No Launchplane tenant environment evidence found for the requested scope."
        )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@launchplane_previews.command("render-index-page")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--output-file", type=click.Path(path_type=Path))
def launchplane_previews_render_index_page(
    state_dir: Path,
    context_name: str,
    output_file: Path | None,
) -> None:
    record_store = _store(state_dir)
    payload = build_preview_inventory_payload(
        record_store=record_store,
        context_name=context_name,
    )
    tenant_payload = _build_launchplane_tenant_payload(
        control_plane_root=_control_plane_root(),
        record_store=record_store,
        context_name=context_name,
    )
    html_output = _render_launchplane_preview_index_page_html(
        payload, tenant_payload=tenant_payload
    )
    if output_file is not None:
        output_file.write_text(html_output, encoding="utf-8")
        return
    click.echo(html_output)


@launchplane_previews.command("render-policy-page")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--output-file", type=click.Path(path_type=Path))
def launchplane_previews_render_policy_page(
    state_dir: Path,
    context_name: str,
    output_file: Path | None,
) -> None:
    record_store = _store(state_dir)
    payload = build_preview_inventory_payload(
        record_store=record_store,
        context_name=context_name,
    )
    html_output = _render_launchplane_preview_policy_page_html(
        payload,
        eligible_contexts=_launchplane_preview_profile_rows(record_store),
    )
    if output_file is not None:
        output_file.write_text(html_output, encoding="utf-8")
        return
    click.echo(html_output)


@launchplane_previews.command("render-site")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--output-dir", type=click.Path(path_type=Path), required=True)
def launchplane_previews_render_site(
    state_dir: Path,
    context_name: str,
    output_dir: Path,
) -> None:
    _write_launchplane_site_bundle(
        state_dir=state_dir,
        output_dir=output_dir,
        context_name=context_name,
    )


@launchplane_previews.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--anchor-repo", required=True)
@click.option("--pr-number", "anchor_pr_number", type=click.IntRange(min=1), required=True)
def launchplane_previews_show(
    state_dir: Path,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> None:
    payload = _require_launchplane_preview_status_payload(
        state_dir=state_dir,
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@launchplane_previews.command("render-status-page")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--anchor-repo", required=True)
@click.option("--pr-number", "anchor_pr_number", type=click.IntRange(min=1), required=True)
@click.option("--output-file", type=click.Path(path_type=Path))
def launchplane_previews_render_status_page(
    state_dir: Path,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
    output_file: Path | None,
) -> None:
    payload = _require_launchplane_preview_status_payload(
        state_dir=state_dir,
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    html_output = _render_launchplane_preview_status_page_html(payload)
    if output_file is not None:
        output_file.write_text(html_output, encoding="utf-8")
        return
    click.echo(html_output)


@launchplane_previews.command("history")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--anchor-repo", required=True)
@click.option("--pr-number", "anchor_pr_number", type=click.IntRange(min=1), required=True)
def launchplane_previews_history(
    state_dir: Path,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> None:
    payload = build_preview_history_payload(
        record_store=_store(state_dir),
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if payload is None:
        raise click.ClickException(
            f"No Launchplane preview found for {context_name}/{anchor_repo}/pr-{anchor_pr_number}."
        )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _read_launchplane_preview_or_fail(
    *,
    record_store: LaunchplanePreviewRecordStore,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> PreviewRecord:
    preview_record = find_preview_record(
        record_store=record_store,
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if preview_record is None:
        raise click.ClickException(
            f"No Launchplane preview found for {context_name}/{anchor_repo}/pr-{anchor_pr_number}."
        )
    return preview_record


def _read_launchplane_generation_or_fail(
    *,
    record_store: LaunchplanePreviewRecordStore,
    preview_id: str,
    generation_id: str,
) -> PreviewGenerationRecord:
    generations = record_store.list_preview_generation_records(preview_id=preview_id)
    for generation_record in generations:
        if generation_record.generation_id == generation_id:
            return generation_record
    raise click.ClickException(
        f"No Launchplane preview generation found for {preview_id} generation {generation_id}."
    )


def _apply_launchplane_request_generation(
    *,
    control_plane_root: Path,
    record_store: LaunchplanePreviewRecordStore,
    preview_request: PreviewMutationRequest,
    generation_request: PreviewGenerationMutationRequest,
) -> dict[str, object]:
    preview_record = _upsert_launchplane_preview_from_request(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=preview_request,
    )
    generation_record = build_preview_generation_record_from_request(
        record_store=record_store,
        request=generation_request,
    )
    transitioned_preview = apply_generation_requested_transition(
        preview=preview_record,
        generation=generation_record,
    )
    generation_path = record_store.write_preview_generation_record(generation_record)
    preview_path = record_store.write_preview_record(transitioned_preview)
    return {
        "generation_id": generation_record.generation_id,
        "generation_path": _record_locator(generation_path, generation_record.generation_id),
        "preview_id": transitioned_preview.preview_id,
        "preview_path": _record_locator(preview_path, transitioned_preview.preview_id),
    }


def _upsert_launchplane_preview_from_request(
    *,
    control_plane_root: Path,
    record_store: LaunchplanePreviewRecordStore,
    request: PreviewMutationRequest,
) -> PreviewRecord:
    return shared_upsert_launchplane_preview_from_request(
        control_plane_root_path=control_plane_root,
        record_store=record_store,
        request=request,
    )


def _apply_launchplane_generation_evidence(
    *,
    control_plane_root: Path,
    record_store: LaunchplanePreviewRecordStore,
    preview_request: PreviewMutationRequest,
    generation_request: PreviewGenerationMutationRequest,
) -> dict[str, object]:
    return shared_apply_launchplane_generation_evidence(
        control_plane_root_path=control_plane_root,
        record_store=record_store,
        preview_request=preview_request,
        generation_request=generation_request,
    )


def _apply_launchplane_destroy_preview(
    *,
    record_store: LaunchplanePreviewRecordStore,
    request: PreviewDestroyMutationRequest,
) -> dict[str, object]:
    return shared_apply_launchplane_destroy_preview(
        record_store=record_store,
        request=request,
    )


def _apply_launchplane_pr_event_intent(
    *,
    control_plane_root: Path,
    record_store: LaunchplanePreviewRecordStore,
    payload: dict[str, object],
) -> dict[str, object]:
    mutation_payload = payload.get("mutation")
    if not isinstance(mutation_payload, dict):
        return {
            "applied": False,
            "reason": "no_mutation_intent",
        }
    intent = LaunchplanePullRequestMutationIntent.model_validate(mutation_payload)
    if intent.command == "request-generation":
        if intent.preview_request is None:
            raise click.ClickException(
                "Resolved Launchplane PR-event request-generation intent is missing preview_request."
            )
        if intent.generation_request is None:
            return {
                "applied": False,
                "reason": "manifest_resolution_required",
            }
        result_payload = _apply_launchplane_request_generation(
            control_plane_root=control_plane_root,
            record_store=record_store,
            preview_request=intent.preview_request,
            generation_request=intent.generation_request,
        )
        return {
            "applied": True,
            "command": intent.command,
            "result": result_payload,
        }
    if intent.destroy_request is None:
        raise click.ClickException(
            "Resolved Launchplane PR-event destroy intent is missing destroy_request."
        )
    result_payload = _apply_launchplane_destroy_preview(
        record_store=record_store,
        request=intent.destroy_request,
    )
    return {
        "applied": True,
        "command": intent.command,
        "result": result_payload,
    }
