import json
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import click

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.dokploy_target_adoption import (
    adopt_dokploy_target,
    create_dokploy_application_target,
)
from control_plane.workflows.provider_target_dual_write import (
    prepare_provider_target_from_dokploy_records,
)
from control_plane.workflows.ship import utc_now_timestamp


_DATABASE_URL_ENV_KEYS = ("LAUNCHPLANE_DATABASE_URL",)
DokployTargetType = Literal["compose", "application"]


def register_dokploy_target_commands(
    main: click.Group,
    *,
    control_plane_root: Callable[[], Path],
    normalize_dokploy_target_type: Callable[[str], DokployTargetType],
    mutate_dokploy_payload_for_target_creation: Callable[..., dict[str, control_plane_dokploy.JsonValue]],
) -> None:
    global _DOKPLOY_TARGET_CALLBACKS
    _DOKPLOY_TARGET_CALLBACKS = _DokployTargetCallbacks(
        control_plane_root=control_plane_root,
        normalize_dokploy_target_type=normalize_dokploy_target_type,
        mutate_dokploy_payload_for_target_creation=mutate_dokploy_payload_for_target_creation,
    )
    main.add_command(dokploy_targets)


@click.group("dokploy-targets")
def dokploy_targets() -> None:
    """Tracked Dokploy target record commands."""


@dokploy_targets.command("list")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane tracked Dokploy target records.",
)
def dokploy_targets_list(database_url: str) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        records = postgres_store.list_dokploy_target_records()
        target_id_records = postgres_store.list_dokploy_target_id_records()
    finally:
        postgres_store.close()
    target_ids_by_route = target_id_map(target_id_records)
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "count": len(records),
                "records": [
                    summarize_dokploy_target_record(
                        record,
                        target_id=target_ids_by_route.get(dokploy_target_route(record), ""),
                    )
                    for record in records
                ],
            },
            indent=2,
            sort_keys=True,
        )
    )


@dokploy_targets.command("show")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane tracked Dokploy target records.",
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
def dokploy_targets_show(database_url: str, context_name: str, instance_name: str) -> None:
    normalized_context = _normalize_dokploy_target_route_value(
        value=context_name, option_name="--context"
    )
    normalized_instance = _normalize_dokploy_target_route_value(
        value=instance_name, option_name="--instance"
    )
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        record = postgres_store.read_dokploy_target_record(
            context_name=normalized_context,
            instance_name=normalized_instance,
        )
        target_id_record = postgres_store.read_dokploy_target_id_record(
            context_name=normalized_context,
            instance_name=normalized_instance,
        )
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            summarize_dokploy_target_record(record, target_id=target_id_record.target_id),
            indent=2,
            sort_keys=True,
        )
    )


@dokploy_targets.command("adopt")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane tracked Dokploy target records.",
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option(
    "--target-type",
    type=click.Choice(["application", "compose"]),
    required=True,
    help="Dokploy target type to adopt.",
)
@click.option("--target-id", required=True, help="Live Dokploy application or compose id.")
@click.option("--project-name", default="", help="Override project name stored on the record.")
@click.option("--target-name", default="", help="Override target name stored on the record.")
@click.option(
    "--source-git-ref",
    default="",
    help="Override source git ref stored on the record. Defaults to the live provider ref or origin/main.",
)
@click.option("--healthcheck-path", default="", help="Healthcheck path stored on the record.")
@click.option("--domain", "domains", multiple=True, help="Domain stored on the record.")
@click.option(
    "--deploy-timeout-seconds",
    type=int,
    default=None,
    help="Optional rollout timeout stored on the record.",
)
@click.option("--source-label", default="cli:dokploy-targets:adopt", show_default=True)
@click.option("--updated-at", default="", help="Override updated timestamp.")
@click.option("--apply", "apply_changes", is_flag=True, help="Write the adopted records.")
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Optional Launchplane repo root used to resolve Dokploy credentials.",
)
def dokploy_targets_adopt(
    database_url: str,
    context_name: str,
    instance_name: str,
    target_type: str,
    target_id: str,
    project_name: str,
    target_name: str,
    source_git_ref: str,
    healthcheck_path: str,
    domains: tuple[str, ...],
    deploy_timeout_seconds: int | None,
    source_label: str,
    updated_at: str,
    apply_changes: bool,
    control_plane_root: Path | None,
) -> None:
    if deploy_timeout_seconds is not None and deploy_timeout_seconds < 1:
        raise click.ClickException("--deploy-timeout-seconds must be at least 1.")
    callbacks = _dokploy_target_callbacks()
    launchplane_root = control_plane_root or callbacks.control_plane_root()
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=launchplane_root)
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        result = adopt_dokploy_target(
            record_store=postgres_store,
            host=host,
            token=token,
            context=context_name,
            instance=instance_name,
            target_type=callbacks.normalize_dokploy_target_type(target_type),
            target_id=target_id,
            project_name=project_name,
            target_name=target_name,
            source_git_ref=source_git_ref,
            healthcheck_path=healthcheck_path,
            domains=domains,
            deploy_timeout_seconds=deploy_timeout_seconds,
            source_label=source_label,
            updated_at=updated_at,
            apply=apply_changes,
            fetch_target_payload=_fetch_dokploy_target_payload_for_adoption,
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "mode": "apply" if result.applied else "dry_run",
                "applied": result.applied,
                "record": summarize_dokploy_target_record(
                    result.target_record,
                    target_id=result.target_id_record.target_id,
                ),
                "provider_fields": result.provider_fields,
                "warnings": list(result.warnings),
            },
            indent=2,
            sort_keys=True,
        )
    )


@dokploy_targets.command("create-application")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane tracked Dokploy target records.",
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option("--target-name", required=True, help="Dokploy application display name.")
@click.option("--project-id", default="", help="Existing Dokploy project id to use.")
@click.option("--project-name", default="", help="Dokploy project name to create or record.")
@click.option("--project-description", default="", help="Optional project description.")
@click.option("--environment-id", default="", help="Existing Dokploy environment id to use.")
@click.option(
    "--environment-name",
    default="",
    help="Dokploy environment name to create; defaults to --instance.",
)
@click.option("--environment-description", default="", help="Optional environment description.")
@click.option("--server-id", default="", help="Optional Dokploy server id for remote apps.")
@click.option("--app-name", default="", help="Optional Dokploy internal app name.")
@click.option("--application-description", default="", help="Optional application description.")
@click.option("--source-git-ref", default="origin/main", show_default=True)
@click.option("--healthcheck-path", default="", help="Healthcheck path stored on the record.")
@click.option("--domain", "domains", multiple=True, help="Domain stored on the record.")
@click.option(
    "--deploy-timeout-seconds",
    type=int,
    default=None,
    help="Optional rollout timeout stored on the record.",
)
@click.option(
    "--source-label",
    default="cli:dokploy-targets:create-application",
    show_default=True,
)
@click.option("--updated-at", default="", help="Override updated timestamp.")
@click.option(
    "--apply", "apply_changes", is_flag=True, help="Create provider target and write records."
)
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Optional Launchplane repo root used to resolve Dokploy credentials.",
)
def dokploy_targets_create_application(
    database_url: str,
    context_name: str,
    instance_name: str,
    target_name: str,
    project_id: str,
    project_name: str,
    project_description: str,
    environment_id: str,
    environment_name: str,
    environment_description: str,
    server_id: str,
    app_name: str,
    application_description: str,
    source_git_ref: str,
    healthcheck_path: str,
    domains: tuple[str, ...],
    deploy_timeout_seconds: int | None,
    source_label: str,
    updated_at: str,
    apply_changes: bool,
    control_plane_root: Path | None,
) -> None:
    if deploy_timeout_seconds is not None and deploy_timeout_seconds < 1:
        raise click.ClickException("--deploy-timeout-seconds must be at least 1.")
    callbacks = _dokploy_target_callbacks()
    launchplane_root = control_plane_root or callbacks.control_plane_root()
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=launchplane_root)
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        result = create_dokploy_application_target(
            record_store=postgres_store,
            host=host,
            token=token,
            context=context_name,
            instance=instance_name,
            target_name=target_name,
            project_id=project_id,
            project_name=project_name,
            project_description=project_description,
            environment_id=environment_id,
            environment_name=environment_name,
            environment_description=environment_description,
            server_id=server_id,
            app_name=app_name,
            application_description=application_description,
            source_git_ref=source_git_ref,
            healthcheck_path=healthcheck_path,
            domains=domains,
            deploy_timeout_seconds=deploy_timeout_seconds,
            source_label=source_label,
            updated_at=updated_at,
            apply=apply_changes,
            mutate_provider=callbacks.mutate_dokploy_payload_for_target_creation,
            fetch_target_payload=_fetch_dokploy_target_payload_for_adoption,
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "mode": "apply" if result.applied else "dry_run",
                "applied": result.applied,
                "plan": result.plan.model_dump(mode="json"),
                "project_id": result.project_id,
                "environment_id": result.environment_id,
                "record": summarize_dokploy_target_record(
                    result.target_record,
                    target_id=result.target_id_record.target_id,
                ),
                "provider_fields": result.provider_fields,
                "provider_requests": list(result.provider_requests),
                "warnings": list(result.warnings),
            },
            indent=2,
            sort_keys=True,
        )
    )


@dokploy_targets.command("put-shopify-protected-store-key")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane tracked Dokploy target records.",
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option(
    "--key",
    "keys",
    multiple=True,
    required=True,
    help="Protected Shopify store key to add for this tracked Dokploy target.",
)
@click.option("--source-label", default="cli", show_default=True)
def dokploy_targets_put_shopify_protected_store_key(
    database_url: str,
    context_name: str,
    instance_name: str,
    keys: tuple[str, ...],
    source_label: str,
) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        existing_records = postgres_store.list_dokploy_target_records()
        target_id_records = postgres_store.list_dokploy_target_id_records()
        record, added_keys, already_present_keys = (
            _build_dokploy_target_record_with_shopify_protected_store_keys_added(
                existing_records=existing_records,
                context_name=context_name,
                instance_name=instance_name,
                keys=keys,
                source_label=source_label,
            )
        )
        target_id_record = _find_dokploy_target_id_record(
            target_id_records=target_id_records,
            context_name=record.context,
            instance_name=record.instance,
        )
        prepare_provider_target_from_dokploy_records(
            record_store=postgres_store,
            target_record=record,
            target_id_record=target_id_record,
        )
        postgres_store.write_dokploy_target_record(record)
        postgres_store.write_provider_target_record(
            record=record_to_provider_target(record=record, target_id_record=target_id_record)
        )
    finally:
        postgres_store.close()
    target_ids_by_route = target_id_map(target_id_records)
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "record": summarize_dokploy_target_record(
                    record,
                    target_id=target_ids_by_route.get(dokploy_target_route(record), ""),
                ),
                "added_keys": added_keys,
                "already_present_keys": already_present_keys,
            },
            indent=2,
            sort_keys=True,
        )
    )


@dokploy_targets.command("unset-shopify-protected-store-key")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane tracked Dokploy target records.",
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option(
    "--key",
    "keys",
    multiple=True,
    required=True,
    help="Protected Shopify store key to remove from this tracked Dokploy target.",
)
@click.option("--source-label", default="cli", show_default=True)
def dokploy_targets_unset_shopify_protected_store_key(
    database_url: str,
    context_name: str,
    instance_name: str,
    keys: tuple[str, ...],
    source_label: str,
) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        existing_records = postgres_store.list_dokploy_target_records()
        target_id_records = postgres_store.list_dokploy_target_id_records()
        record, removed_keys, missing_keys = (
            _build_dokploy_target_record_with_shopify_protected_store_keys_removed(
                existing_records=existing_records,
                context_name=context_name,
                instance_name=instance_name,
                keys=keys,
                source_label=source_label,
            )
        )
        target_id_record = _find_dokploy_target_id_record(
            target_id_records=target_id_records,
            context_name=record.context,
            instance_name=record.instance,
        )
        prepare_provider_target_from_dokploy_records(
            record_store=postgres_store,
            target_record=record,
            target_id_record=target_id_record,
        )
        postgres_store.write_dokploy_target_record(record)
        postgres_store.write_provider_target_record(
            record=record_to_provider_target(record=record, target_id_record=target_id_record)
        )
    finally:
        postgres_store.close()
    target_ids_by_route = target_id_map(target_id_records)
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "record": summarize_dokploy_target_record(
                    record,
                    target_id=target_ids_by_route.get(dokploy_target_route(record), ""),
                ),
                "removed_keys": removed_keys,
                "missing_keys": missing_keys,
            },
            indent=2,
            sort_keys=True,
        )
    )


@dokploy_targets.command("relabel")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane tracked Dokploy target records.",
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option("--source-label", required=True)
def dokploy_targets_relabel(
    database_url: str, context_name: str, instance_name: str, source_label: str
) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        existing_records = postgres_store.list_dokploy_target_records()
        target_id_records = postgres_store.list_dokploy_target_id_records()
        record = _build_dokploy_target_record_for_relabel(
            existing_records=existing_records,
            context_name=context_name,
            instance_name=instance_name,
            source_label=source_label,
        )
        target_id_record = _find_dokploy_target_id_record(
            target_id_records=target_id_records,
            context_name=record.context,
            instance_name=record.instance,
        )
        prepare_provider_target_from_dokploy_records(
            record_store=postgres_store,
            target_record=record,
            target_id_record=target_id_record,
        )
        postgres_store.write_dokploy_target_record(record)
        postgres_store.write_provider_target_record(
            record=record_to_provider_target(record=record, target_id_record=target_id_record)
        )
    finally:
        postgres_store.close()
    target_ids_by_route = target_id_map(target_id_records)
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "record": summarize_dokploy_target_record(
                    record,
                    target_id=target_ids_by_route.get(dokploy_target_route(record), ""),
                ),
            },
            indent=2,
            sort_keys=True,
        )
    )


def _normalize_dokploy_target_route_value(*, value: str, option_name: str) -> str:
    normalized_value = value.strip().lower()
    if not normalized_value:
        raise click.ClickException(f"Dokploy target {option_name} must be non-empty.")
    return normalized_value


def _normalize_shopify_protected_store_key(raw_value: str) -> str:
    normalized_value = raw_value.strip().lower()
    if not normalized_value:
        raise click.ClickException("Shopify protected store keys must be non-empty.")
    return normalized_value


def dokploy_target_route(record: DokployTargetRecord | DokployTargetIdRecord) -> tuple[str, str]:
    return (record.context.strip().lower(), record.instance.strip().lower())


def _find_dokploy_target_record(
    *,
    existing_records: tuple[DokployTargetRecord, ...],
    context_name: str,
    instance_name: str,
) -> DokployTargetRecord | None:
    normalized_route = (context_name.strip().lower(), instance_name.strip().lower())
    for record in existing_records:
        if dokploy_target_route(record) == normalized_route:
            return record
    return None


def _find_dokploy_target_id_record(
    *,
    target_id_records: tuple[DokployTargetIdRecord, ...],
    context_name: str,
    instance_name: str,
) -> DokployTargetIdRecord:
    normalized_route = (context_name.strip().lower(), instance_name.strip().lower())
    for record in target_id_records:
        if dokploy_target_route(record) == normalized_route:
            return record
    raise click.ClickException(
        "Missing DB-backed tracked Dokploy target-id record for the requested route."
    )


def target_id_map(
    target_id_records: tuple[DokployTargetIdRecord, ...],
) -> dict[tuple[str, str], str]:
    return {dokploy_target_route(record): record.target_id for record in target_id_records}


def record_to_provider_target(
    *, record: DokployTargetRecord, target_id_record: DokployTargetIdRecord
) -> ProviderTargetRecord:
    return ProviderTargetRecord.from_dokploy_records(
        target_record=record,
        target_id_record=target_id_record,
    )


def summarize_dokploy_target_record(
    record: DokployTargetRecord,
    *,
    target_id: str = "",
) -> dict[str, object]:
    return {
        "context": record.context,
        "instance": record.instance,
        "target_id": target_id,
        "target_type": record.target_type,
        "project_name": record.project_name,
        "target_name": record.target_name,
        "source_git_ref": record.source_git_ref,
        "compose_path": record.compose_path,
        "watch_paths": list(record.watch_paths),
        "domains": list(record.domains),
        "require_test_gate": record.require_test_gate,
        "require_prod_gate": record.require_prod_gate,
        "healthcheck_enabled": record.healthcheck_enabled,
        "healthcheck_path": record.healthcheck_path,
        "env_keys": sorted(record.env.keys()),
        "env_value_count": len(record.env),
        "shopify_protected_store_keys": sorted(record.policies.shopify.protected_store_keys),
        "updated_at": record.updated_at,
        "source_label": record.source_label,
    }


def _fetch_dokploy_target_payload_for_adoption(
    host: str,
    token: str,
    target_type: str,
    target_id: str,
) -> dict[str, control_plane_dokploy.JsonValue]:
    return control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )


def _clone_dokploy_target_record(
    *,
    target_record: DokployTargetRecord,
    source_label: str,
    policies_payload: dict[str, object] | None = None,
) -> DokployTargetRecord:
    payload = target_record.model_dump(mode="python")
    if policies_payload is not None:
        payload["policies"] = policies_payload
    payload["updated_at"] = utc_now_timestamp()
    payload["source_label"] = source_label.strip() or "cli"
    return DokployTargetRecord.model_validate(payload)


def _build_dokploy_target_record_with_shopify_protected_store_keys_added(
    *,
    existing_records: tuple[DokployTargetRecord, ...],
    context_name: str,
    instance_name: str,
    keys: tuple[str, ...],
    source_label: str,
) -> tuple[DokployTargetRecord, tuple[str, ...], tuple[str, ...]]:
    normalized_context = _normalize_dokploy_target_route_value(
        value=context_name, option_name="--context"
    )
    normalized_instance = _normalize_dokploy_target_route_value(
        value=instance_name, option_name="--instance"
    )
    target_record = _find_dokploy_target_record(
        existing_records=existing_records,
        context_name=normalized_context,
        instance_name=normalized_instance,
    )
    if target_record is None:
        raise click.ClickException(
            "Missing DB-backed tracked Dokploy target record for the requested route."
        )

    requested_keys = tuple(_normalize_shopify_protected_store_key(raw_value) for raw_value in keys)
    current_keys = list(target_record.policies.shopify.protected_store_keys)
    current_key_set = {key.strip().lower() for key in current_keys}
    added_keys: list[str] = []
    already_present_keys: list[str] = []
    for requested_key in requested_keys:
        if requested_key in current_key_set:
            already_present_keys.append(requested_key)
            continue
        current_keys.append(requested_key)
        current_key_set.add(requested_key)
        added_keys.append(requested_key)

    record = _clone_dokploy_target_record(
        target_record=target_record,
        source_label=source_label,
        policies_payload={
            "shopify": {
                "protected_store_keys": current_keys,
            }
        },
    )
    return record, tuple(sorted(added_keys)), tuple(sorted(already_present_keys))


def _build_dokploy_target_record_with_shopify_protected_store_keys_removed(
    *,
    existing_records: tuple[DokployTargetRecord, ...],
    context_name: str,
    instance_name: str,
    keys: tuple[str, ...],
    source_label: str,
) -> tuple[DokployTargetRecord, tuple[str, ...], tuple[str, ...]]:
    normalized_context = _normalize_dokploy_target_route_value(
        value=context_name, option_name="--context"
    )
    normalized_instance = _normalize_dokploy_target_route_value(
        value=instance_name, option_name="--instance"
    )
    target_record = _find_dokploy_target_record(
        existing_records=existing_records,
        context_name=normalized_context,
        instance_name=normalized_instance,
    )
    if target_record is None:
        raise click.ClickException(
            "Missing DB-backed tracked Dokploy target record for the requested route."
        )

    requested_keys = tuple(_normalize_shopify_protected_store_key(raw_value) for raw_value in keys)
    current_keys = list(target_record.policies.shopify.protected_store_keys)
    current_key_set = {key.strip().lower() for key in current_keys}
    removed_keys: list[str] = []
    missing_keys: list[str] = []
    for requested_key in requested_keys:
        if requested_key not in current_key_set:
            missing_keys.append(requested_key)
            continue
        current_keys = [
            existing_key
            for existing_key in current_keys
            if existing_key.strip().lower() != requested_key
        ]
        current_key_set.remove(requested_key)
        removed_keys.append(requested_key)

    record = _clone_dokploy_target_record(
        target_record=target_record,
        source_label=source_label,
        policies_payload={
            "shopify": {
                "protected_store_keys": current_keys,
            }
        },
    )
    return record, tuple(sorted(removed_keys)), tuple(sorted(missing_keys))


def _build_dokploy_target_record_for_relabel(
    *,
    existing_records: tuple[DokployTargetRecord, ...],
    context_name: str,
    instance_name: str,
    source_label: str,
) -> DokployTargetRecord:
    normalized_context = _normalize_dokploy_target_route_value(
        value=context_name, option_name="--context"
    )
    normalized_instance = _normalize_dokploy_target_route_value(
        value=instance_name, option_name="--instance"
    )
    target_record = _find_dokploy_target_record(
        existing_records=existing_records,
        context_name=normalized_context,
        instance_name=normalized_instance,
    )
    if target_record is None:
        raise click.ClickException(
            "Missing DB-backed tracked Dokploy target record for the requested route."
        )
    return _clone_dokploy_target_record(target_record=target_record, source_label=source_label)


class _DokployTargetCallbacks:
    def __init__(
        self,
        *,
        control_plane_root: Callable[[], Path],
        normalize_dokploy_target_type: Callable[[str], DokployTargetType],
        mutate_dokploy_payload_for_target_creation: Callable[..., dict[str, control_plane_dokploy.JsonValue]],
    ) -> None:
        self.control_plane_root = control_plane_root
        self.normalize_dokploy_target_type = normalize_dokploy_target_type
        self.mutate_dokploy_payload_for_target_creation = mutate_dokploy_payload_for_target_creation


_DOKPLOY_TARGET_CALLBACKS: _DokployTargetCallbacks | None = None


def _dokploy_target_callbacks() -> _DokployTargetCallbacks:
    if _DOKPLOY_TARGET_CALLBACKS is None:
        raise click.ClickException("Dokploy target CLI callbacks were not registered.")
    return _DOKPLOY_TARGET_CALLBACKS
