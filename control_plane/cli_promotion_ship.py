import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import click

from control_plane.cli_shared import (
    DATABASE_URL_ENV_KEYS as _DATABASE_URL_ENV_KEYS,
    direct_db_mutation_acknowledgement_option as _direct_db_mutation_acknowledgement_option,
    require_direct_db_mutation_acknowledgement as _require_direct_db_mutation_acknowledgement,
)
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import PromotionRequest
from control_plane.contracts.ship_request import ShipRequest
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.promote import (
    build_executed_promotion_record,
    build_promotion_record,
    generate_promotion_record_id,
)


@dataclass(frozen=True)
class PromotionShipCliCallbacks:
    store_factory: Callable[..., FilesystemRecordStore | PostgresRecordStore]
    load_json_file: Callable[[Path], dict[str, object]]
    normalize_dokploy_target_type: Callable[[str], Literal["compose", "application"]]
    resolve_native_promotion_request: Callable[..., PromotionRequest]
    require_artifact_id: Callable[..., str]
    resolve_backup_gate_for_promotion: Callable[..., tuple[PromotionRequest, object | None]]
    read_artifact_manifest: Callable[..., object]
    read_source_release_tuple_for_promotion: Callable[..., object]
    resolve_ship_request_for_promotion: Callable[..., ShipRequest]
    execute_ship: Callable[..., tuple[Path | None, DeploymentRecord | ShipRequest]]
    write_environment_inventory: Callable[..., Path | None]
    write_promoted_release_tuple: Callable[..., Path | None]
    resolve_native_ship_request: Callable[..., ShipRequest]


_callbacks: PromotionShipCliCallbacks | None = None


def register_promotion_ship_commands(
    main: click.Group, *, callbacks: PromotionShipCliCallbacks
) -> None:
    global _callbacks
    _callbacks = callbacks
    main.add_command(promote)
    main.add_command(ship)


def _promotion_ship_callbacks() -> PromotionShipCliCallbacks:
    if _callbacks is None:
        raise click.ClickException("Launchplane promotion/ship CLI callbacks are not configured.")
    return _callbacks


def _store(
    state_dir: Path, *, database_url: str | None = None
) -> FilesystemRecordStore | PostgresRecordStore:
    return _promotion_ship_callbacks().store_factory(state_dir, database_url=database_url)


def _resolve_execution_database_url(
    *, database_url: str, local_rehearsal: bool, allow_direct_db_mutation: bool
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
            "Promotion and ship execution require --database-url or "
            "LAUNCHPLANE_DATABASE_URL. Use --local-rehearsal for explicit "
            "local filesystem rehearsal."
        )
    _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)
    return normalized_database_url


def _load_json_file(input_file: Path) -> dict[str, object]:
    return _promotion_ship_callbacks().load_json_file(input_file)


def _normalize_dokploy_target_type(target_type: str) -> Literal["compose", "application"]:
    return _promotion_ship_callbacks().normalize_dokploy_target_type(target_type)


def _resolve_native_promotion_request(**kwargs: object) -> PromotionRequest:
    return _promotion_ship_callbacks().resolve_native_promotion_request(**kwargs)


def _require_artifact_id(**kwargs: object) -> str:
    return _promotion_ship_callbacks().require_artifact_id(**kwargs)


def _resolve_backup_gate_for_promotion(**kwargs: object) -> tuple[PromotionRequest, object | None]:
    return _promotion_ship_callbacks().resolve_backup_gate_for_promotion(**kwargs)


def _read_artifact_manifest(**kwargs: object) -> object:
    return _promotion_ship_callbacks().read_artifact_manifest(**kwargs)


def _read_source_release_tuple_for_promotion(**kwargs: object) -> object:
    return _promotion_ship_callbacks().read_source_release_tuple_for_promotion(**kwargs)


def _resolve_ship_request_for_promotion(**kwargs: object) -> ShipRequest:
    return _promotion_ship_callbacks().resolve_ship_request_for_promotion(**kwargs)


def _execute_ship(**kwargs: object) -> tuple[Path | None, DeploymentRecord | ShipRequest]:
    return _promotion_ship_callbacks().execute_ship(**kwargs)


def _write_environment_inventory(**kwargs: object) -> Path | None:
    return _promotion_ship_callbacks().write_environment_inventory(**kwargs)


def _write_promoted_release_tuple(**kwargs: object) -> Path | None:
    return _promotion_ship_callbacks().write_promoted_release_tuple(**kwargs)


def _resolve_native_ship_request(**kwargs: object) -> ShipRequest:
    return _promotion_ship_callbacks().resolve_native_ship_request(**kwargs)


@click.group()
def promote() -> None:
    """Promotion workflow commands."""


@promote.command("record")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--record-id", required=True)
@click.option("--artifact-id", required=True)
@click.option("--backup-record-id", default="", show_default=False)
@click.option("--context", "context_name", required=True)
@click.option("--from-instance", "from_instance_name", required=True)
@click.option("--to-instance", "to_instance_name", required=True)
@click.option("--target-name", required=True)
@click.option("--target-type", type=click.Choice(["compose", "application"]), required=True)
@click.option("--deploy-mode", required=True)
@click.option("--deployment-id", default="", show_default=False)
def promote_record(
    state_dir: Path,
    database_url: str,
    record_id: str,
    artifact_id: str,
    backup_record_id: str,
    context_name: str,
    from_instance_name: str,
    to_instance_name: str,
    target_name: str,
    target_type: str,
    deploy_mode: str,
    deployment_id: str,
) -> None:
    record = build_promotion_record(
        record_id=record_id,
        artifact_id=artifact_id,
        backup_record_id=backup_record_id,
        context_name=context_name,
        from_instance_name=from_instance_name,
        to_instance_name=to_instance_name,
        target_name=target_name,
        target_type=_normalize_dokploy_target_type(target_type),
        deploy_mode=deploy_mode,
        deployment_id=deployment_id,
    )
    record_path = _store(state_dir, database_url=database_url).write_promotion_record(record)
    click.echo(record_path)


@promote.command("resolve")
@click.option("--context", "context_name", required=True)
@click.option("--from-instance", "from_instance_name", required=True)
@click.option("--to-instance", "to_instance_name", required=True)
@click.option("--artifact-id", required=True)
@click.option("--backup-record-id", required=True)
@click.option("--source-ref", "source_git_ref", default="")
@click.option("--wait/--no-wait", default=True, show_default=True)
@click.option("--timeout", "timeout_override_seconds", type=int, default=None)
@click.option("--verify-health/--no-verify-health", default=True)
@click.option("--health-timeout", "health_timeout_override_seconds", type=int, default=None)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--no-cache", is_flag=True, default=False)
@click.option("--allow-dirty", is_flag=True, default=False)
def promote_resolve(
    context_name: str,
    from_instance_name: str,
    to_instance_name: str,
    artifact_id: str,
    backup_record_id: str,
    source_git_ref: str,
    wait: bool,
    timeout_override_seconds: int | None,
    verify_health: bool,
    health_timeout_override_seconds: int | None,
    dry_run: bool,
    no_cache: bool,
    allow_dirty: bool,
) -> None:
    request = _resolve_native_promotion_request(
        context_name=context_name,
        from_instance_name=from_instance_name,
        to_instance_name=to_instance_name,
        artifact_id=artifact_id,
        backup_record_id=backup_record_id,
        source_git_ref=source_git_ref,
        wait=wait,
        timeout_override_seconds=timeout_override_seconds,
        verify_health=verify_health,
        health_timeout_override_seconds=health_timeout_override_seconds,
        dry_run=dry_run,
        no_cache=no_cache,
        allow_dirty=allow_dirty,
    )
    click.echo(json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True))


@promote.command("execute")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--env-file", type=click.Path(exists=True, path_type=Path), default=None)
def promote_execute(
    state_dir: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
    input_file: Path,
    env_file: Path | None,
) -> None:
    execution_database_url = _resolve_execution_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
    )
    request = PromotionRequest.model_validate(_load_json_file(input_file))
    record_store = _store(state_dir, database_url=execution_database_url)
    resolved_artifact_id = _require_artifact_id(requested_artifact_id=request.artifact_id)
    _read_artifact_manifest(
        record_store=record_store,
        artifact_id=resolved_artifact_id,
    )
    normalized_request = request.model_copy(update={"artifact_id": resolved_artifact_id})
    resolved_request, _backup_gate_record = _resolve_backup_gate_for_promotion(
        request=normalized_request,
        record_store=record_store,
    )
    source_release_tuple = _read_source_release_tuple_for_promotion(
        record_store=record_store,
        request=resolved_request,
    )
    record_id = generate_promotion_record_id(
        context_name=resolved_request.context,
        from_instance_name=resolved_request.from_instance,
        to_instance_name=resolved_request.to_instance,
    )
    if resolved_request.dry_run:
        _resolve_ship_request_for_promotion(request=resolved_request)
        click.echo(
            json.dumps(
                build_executed_promotion_record(
                    request=resolved_request,
                    record_id=record_id,
                    deployment_id="",
                    deployment_status="pending",
                ).model_dump(mode="json"),
                indent=2,
                sort_keys=True,
            )
        )
        return

    pending_record = build_executed_promotion_record(
        request=resolved_request,
        record_id=record_id,
        deployment_id="",
        deployment_status="pending",
    )
    record_path = record_store.write_promotion_record(pending_record)

    try:
        ship_request = _resolve_ship_request_for_promotion(request=resolved_request)
        _record_path, deployment_record = _execute_ship(
            state_dir=state_dir,
            database_url=execution_database_url,
            env_file=env_file,
            request=ship_request,
            mint_release_tuple=False,
        )
        if not isinstance(deployment_record, DeploymentRecord):
            raise click.ClickException(
                "Ship execution returned an unexpected non-record payload during promotion."
            )
        final_record = build_executed_promotion_record(
            request=resolved_request,
            record_id=record_id,
            deployment_record_id=deployment_record.record_id,
            deployment_id=deployment_record.deploy.deployment_id,
            deployment_status=deployment_record.deploy.status,
        )
    except (subprocess.CalledProcessError, click.ClickException, json.JSONDecodeError):
        final_record = build_executed_promotion_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="fail",
        )
        record_store.write_promotion_record(final_record)
        raise

    record_store.write_promotion_record(final_record)
    if deployment_record.wait_for_completion and deployment_record.deploy.status == "pass":
        _write_environment_inventory(
            record_store=record_store,
            deployment_record=deployment_record,
            promotion_record_id=final_record.record_id,
            promoted_from_instance=final_record.from_instance,
        )
        _write_promoted_release_tuple(
            record_store=record_store,
            source_tuple=source_release_tuple,
            deployment_record=deployment_record,
            promotion_record=final_record,
        )
    click.echo(record_path)


@click.group()
def ship() -> None:
    """Ship workflow commands."""


@ship.command("plan")
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def ship_plan(input_file: Path) -> None:
    request = ShipRequest.model_validate(_load_json_file(input_file))
    click.echo(json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True))


@ship.command("resolve")
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option("--artifact-id", required=True)
@click.option("--source-ref", "source_git_ref", default="")
@click.option("--wait/--no-wait", default=True, show_default=True)
@click.option("--timeout", "timeout_override_seconds", type=int, default=None)
@click.option("--verify-health/--no-verify-health", default=True)
@click.option("--health-timeout", "health_timeout_override_seconds", type=int, default=None)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--no-cache", is_flag=True, default=False)
@click.option("--allow-dirty", is_flag=True, default=False)
def ship_resolve(
    context_name: str,
    instance_name: str,
    artifact_id: str,
    source_git_ref: str,
    wait: bool,
    timeout_override_seconds: int | None,
    verify_health: bool,
    health_timeout_override_seconds: int | None,
    dry_run: bool,
    no_cache: bool,
    allow_dirty: bool,
) -> None:
    request = _resolve_native_ship_request(
        context_name=context_name,
        instance_name=instance_name,
        artifact_id=artifact_id,
        source_git_ref=source_git_ref,
        wait=wait,
        timeout_override_seconds=timeout_override_seconds,
        verify_health=verify_health,
        health_timeout_override_seconds=health_timeout_override_seconds,
        dry_run=dry_run,
        no_cache=no_cache,
        allow_dirty=allow_dirty,
    )
    click.echo(json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True))


@ship.command("execute")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@_direct_db_mutation_acknowledgement_option
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--env-file", type=click.Path(exists=True, path_type=Path), default=None)
def ship_execute(
    state_dir: Path,
    database_url: str,
    local_rehearsal: bool,
    allow_direct_db_mutation: bool,
    input_file: Path,
    env_file: Path | None,
) -> None:
    execution_database_url = _resolve_execution_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        allow_direct_db_mutation=allow_direct_db_mutation,
    )
    request = ShipRequest.model_validate(_load_json_file(input_file))
    record_path, _record = _execute_ship(
        state_dir=state_dir,
        database_url=execution_database_url,
        env_file=env_file,
        request=request,
    )
    if record_path is not None:
        click.echo(record_path)
