import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import click

from control_plane.cli_shared import (
    DATABASE_URL_ENV_KEYS as _DATABASE_URL_ENV_KEYS,
    direct_db_mutation_acknowledgement_option as _direct_db_mutation_acknowledgement_option,
    require_direct_db_mutation_acknowledgement as _require_direct_db_mutation_acknowledgement,
)
from control_plane.contracts.preview_enablement_record import PreviewEnablementRecord
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_mutation_request import (
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.launchplane_mutations import (
    apply_launchplane_destroy_preview as shared_apply_launchplane_destroy_preview,
    apply_launchplane_generation_evidence as shared_apply_launchplane_generation_evidence,
    upsert_launchplane_preview_from_request as shared_upsert_launchplane_preview_from_request,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.launchplane import ProductProfileListStore
from control_plane.workflows.launchplane import (
    apply_generation_failed_transition,
    apply_generation_ready_transition,
    apply_generation_requested_transition,
    build_preview_generation_record_from_request,
    build_preview_history_payload,
    build_preview_inventory_payload,
    build_preview_record_from_request,
    find_preview_record,
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
    _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)
    return normalized_database_url


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
