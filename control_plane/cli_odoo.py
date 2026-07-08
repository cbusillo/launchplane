import json
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from json import JSONDecodeError
from pathlib import Path
from typing import Literal, cast

import click
from pydantic import ValidationError

from control_plane import odoo_instance_overrides as control_plane_odoo_instance_overrides
from control_plane import secrets as control_plane_secrets
from control_plane.odoo_ownership_checks import (
    render_odoo_ownership_scan_json,
    render_odoo_ownership_scan_text,
    scan_odoo_ownership_boundaries,
)
from control_plane.contracts.odoo_instance_override_record import OdooAddonSettingOverride
from control_plane.contracts.odoo_instance_override_record import OdooConfigParameterOverride
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import OdooOverrideApplyResult
from control_plane.contracts.odoo_instance_override_record import OdooOverrideValue
from control_plane.contracts.odoo_instance_override_record import OdooWebsiteBootstrapPayload
from control_plane.contracts.odoo_instance_override_record import (
    validate_odoo_website_bootstrap_contract,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementRequest,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.workflows.odoo_artifact_publish import (
    OdooArtifactPublishRequest,
    OdooArtifactPublishStore,
    execute_odoo_artifact_publish,
)
from control_plane.workflows.odoo_prod_backup_gate import (
    OdooProdBackupGateRequest,
    execute_odoo_prod_backup_gate,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.odoo_stable_target_replacement import (
    build_odoo_stable_target_replacement_plan,
)
from control_plane.workflows.ship import utc_now_timestamp


_DATABASE_URL_ENV_KEYS = ("LAUNCHPLANE_DATABASE_URL",)
_SECRET_SHAPED_RUNTIME_ENV_KEY_PARTS = {"PASSWORD", "TOKEN", "SECRET", "KEY"}
_DIRECT_DB_MUTATION_MESSAGE = (
    "Direct local DB mutation is restricted after the Launchplane service boundary. "
    "Use the deployed service route or operator workflow for shared/production changes, "
    "or pass --allow-direct-db-mutation only for explicit local/bootstrap repair."
)
OdooOverrideApplyStatus = Literal["skipped", "pending", "pass", "fail"]


@dataclass(frozen=True)
class OdooCliCallbacks:
    control_plane_root: Callable[[], Path]
    store_factory: Callable[..., object]
    normalize_odoo_apply_status: Callable[[str], OdooOverrideApplyStatus]
    read_dokploy_config: Callable[..., tuple[str, str]]


_callbacks: OdooCliCallbacks | None = None


def register_odoo_commands(main: click.Group, *, callbacks: OdooCliCallbacks) -> None:
    global _callbacks
    _callbacks = callbacks
    main.add_command(odoo_artifacts)
    main.add_command(odoo_backup_gates)
    main.add_command(odoo_overrides)
    main.add_command(odoo_targets)
    main.add_command(odoo_ownership)


def _odoo_callbacks() -> OdooCliCallbacks:
    if _callbacks is None:
        raise click.ClickException("Launchplane Odoo CLI callbacks are not configured.")
    return _callbacks


def _control_plane_root() -> Path:
    return _odoo_callbacks().control_plane_root()


def _store(state_dir: Path, *, database_url: str | None = None) -> object:
    return _odoo_callbacks().store_factory(state_dir, database_url=database_url)


def normalize_odoo_apply_status(status: str) -> OdooOverrideApplyStatus:
    return _odoo_callbacks().normalize_odoo_apply_status(status)


def _read_dokploy_config(*, control_plane_root: Path, database_url: str) -> tuple[str, str]:
    return _odoo_callbacks().read_dokploy_config(
        control_plane_root=control_plane_root, database_url=database_url
    )


def _direct_db_mutation_acknowledgement_option(
    function: Callable[..., object],
) -> Callable[..., object]:
    return click.option(
        "--allow-direct-db-mutation",
        is_flag=True,
        help="Acknowledge direct local DB mutation for explicit local/bootstrap repair.",
    )(function)


def _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation: bool) -> None:
    if not allow_direct_db_mutation:
        raise click.ClickException(_DIRECT_DB_MUTATION_MESSAGE)


def _relabel_odoo_override_secret_binding(
    *,
    record_store: PostgresRecordStore,
    binding: SecretBinding,
    desired_binding_key: str,
    apply_changes: bool,
    source_label: str,
) -> tuple[str, str]:
    desired_binding_id = control_plane_secrets.expected_secret_binding_id(
        secret_id=binding.secret_id, binding_key=desired_binding_key
    )
    action = "unchanged" if binding.binding_key == desired_binding_key else "pending"
    if apply_changes:
        result = control_plane_secrets.relabel_secret_binding(
            record_store=record_store,
            binding_id=binding.binding_id,
            binding_key=desired_binding_key,
            actor=source_label,
            source_label=source_label,
        )
        desired_binding_id = result["binding_id"]
        action = result["action"]
    return desired_binding_id, action


@click.group("odoo-artifacts")
def odoo_artifacts() -> None:
    """Odoo artifact publish driver commands."""


@click.group("odoo-backup-gates")
def odoo_backup_gates() -> None:
    """Odoo backup-gate driver commands."""


@odoo_artifacts.command("publish")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane artifact and runtime-environment records.",
)
@click.option("--context", required=True)
@click.option("--instance", default="testing", show_default=True)
@click.option("--manifest", "manifest_path", type=click.Path(path_type=Path), required=True)
@click.option("--devkit-root", type=click.Path(path_type=Path), required=True)
@click.option("--image-repository", required=True)
@click.option("--image-tag", required=True)
@click.option("--platform", multiple=True, help="Target platform passed to odoo-devkit publish.")
@click.option("--output-file", type=click.Path(path_type=Path), default=None)
@click.option("--no-cache", is_flag=True, default=False)
def odoo_artifacts_publish(
    database_url: str,
    context: str,
    instance: str,
    manifest_path: Path,
    devkit_root: Path,
    image_repository: str,
    image_tag: str,
    platform: tuple[str, ...],
    output_file: Path | None,
    no_cache: bool,
) -> None:
    record_store = _store(Path("state"), database_url=database_url)
    result = execute_odoo_artifact_publish(
        control_plane_root=_control_plane_root(),
        record_store=cast(OdooArtifactPublishStore, record_store),
        request=OdooArtifactPublishRequest(
            context=context,
            instance=instance,
            manifest_path=manifest_path,
            devkit_root=devkit_root,
            image_repository=image_repository,
            image_tag=image_tag,
            platforms=platform,
            output_file=output_file,
            no_cache=no_cache,
        ),
    )
    click.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    if result.status != "pass":
        raise click.ClickException(result.error_message or "Odoo artifact publish failed.")


@odoo_backup_gates.command("capture")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane backup-gate and Dokploy target records.",
)
@click.option("--context", required=True)
@click.option("--instance", default="prod", show_default=True)
@click.option("--backup-record-id", required=True)
@click.option("--timeout", "timeout_seconds", type=int, default=None)
def odoo_backup_gates_capture(
    database_url: str,
    context: str,
    instance: str,
    backup_record_id: str,
    timeout_seconds: int | None,
) -> None:
    result = execute_odoo_prod_backup_gate(
        control_plane_root=_control_plane_root(),
        record_store=_store(Path("state"), database_url=database_url),
        request=OdooProdBackupGateRequest(
            context=context,
            instance=instance,
            backup_record_id=backup_record_id,
            timeout_seconds=timeout_seconds,
        ),
    )
    click.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    if result.backup_status != "pass":
        raise click.ClickException(result.error_message or "Odoo backup gate failed.")


def _summarize_odoo_instance_override_record(
    record: OdooInstanceOverrideRecord,
) -> dict[str, object]:
    return {
        "context": record.context,
        "instance": record.instance,
        "updated_at": record.updated_at,
        "source_label": record.source_label,
        "apply_on": record.apply_on,
        "last_apply_status": record.last_apply.status,
        "last_apply_at": record.last_apply.applied_at,
        "config_parameter_keys": sorted(override.key for override in record.config_parameters),
        "addon_settings": sorted(
            f"{override.addon}.{override.setting}" for override in record.addon_settings
        ),
        "config_parameter_count": len(record.config_parameters),
        "addon_setting_count": len(record.addon_settings),
        "website_bootstrap": record.website_bootstrap is not None,
    }


def _odoo_override_name_requires_secret_store(key_name: str) -> bool:
    normalized_parts = key_name.replace(".", "_").replace("-", "_").upper().split("_")
    return any(key_part in _SECRET_SHAPED_RUNTIME_ENV_KEY_PARTS for key_part in normalized_parts)


def _build_odoo_override_value(
    *,
    value: str | None,
    secret_binding_id: str,
    value_name: str,
) -> OdooOverrideValue:
    normalized_secret_binding_id = secret_binding_id.strip()
    if value is None and not normalized_secret_binding_id:
        raise click.ClickException("Provide either --value or --secret-binding-id.")
    if value is not None and normalized_secret_binding_id:
        raise click.ClickException("Provide only one of --value or --secret-binding-id.")
    if value is not None:
        if _odoo_override_name_requires_secret_store(value_name):
            raise click.ClickException(
                f"Odoo override {value_name!r} looks secret-shaped and must use --secret-binding-id."
            )
        return OdooOverrideValue(source="literal", value=value)
    return OdooOverrideValue(
        source="secret_binding", secret_binding_id=normalized_secret_binding_id
    )


def _find_odoo_instance_override_record(
    *,
    existing_records: tuple[OdooInstanceOverrideRecord, ...],
    context_name: str,
    instance_name: str,
) -> OdooInstanceOverrideRecord | None:
    for record in existing_records:
        if record.context == context_name and record.instance == instance_name:
            return record
    return None


def _build_odoo_instance_override_record_with_config_parameter(
    *,
    existing_records: tuple[OdooInstanceOverrideRecord, ...],
    context_name: str,
    instance_name: str,
    key_name: str,
    override_value: OdooOverrideValue,
    source_label: str,
) -> OdooInstanceOverrideRecord:
    normalized_context = context_name.strip().lower()
    normalized_instance = instance_name.strip().lower()
    normalized_key = key_name.strip().lower()
    if not normalized_context or not normalized_instance:
        raise click.ClickException(
            "Odoo instance override records require --context and --instance."
        )
    if not normalized_key:
        raise click.ClickException("Odoo config parameter overrides require --key.")
    target_record = _find_odoo_instance_override_record(
        existing_records=existing_records,
        context_name=normalized_context,
        instance_name=normalized_instance,
    )
    config_parameters = {
        override.key: override
        for override in (target_record.config_parameters if target_record is not None else ())
    }
    addon_settings = target_record.addon_settings if target_record is not None else ()
    config_parameters[normalized_key] = OdooConfigParameterOverride(
        key=normalized_key, value=override_value
    )
    apply_on = target_record.apply_on if target_record is not None else ()
    apply_phases = tuple(dict.fromkeys((*apply_on, "deploy", "promotion")))
    return OdooInstanceOverrideRecord(
        context=normalized_context,
        instance=normalized_instance,
        apply_on=apply_phases,
        config_parameters=tuple(config_parameters[key] for key in sorted(config_parameters)),
        addon_settings=addon_settings,
        website_bootstrap=target_record.website_bootstrap if target_record is not None else None,
        updated_at=utc_now_timestamp(),
        source_label=source_label.strip() or "cli",
    )


def _build_odoo_instance_override_record_with_addon_setting(
    *,
    existing_records: tuple[OdooInstanceOverrideRecord, ...],
    context_name: str,
    instance_name: str,
    addon_name: str,
    setting_name: str,
    override_value: OdooOverrideValue,
    source_label: str,
) -> OdooInstanceOverrideRecord:
    normalized_context = context_name.strip().lower()
    normalized_instance = instance_name.strip().lower()
    normalized_addon = addon_name.strip().lower()
    normalized_setting = setting_name.strip().lower()
    if not normalized_context or not normalized_instance:
        raise click.ClickException(
            "Odoo instance override records require --context and --instance."
        )
    if not normalized_addon or not normalized_setting:
        raise click.ClickException("Odoo addon setting overrides require --addon and --setting.")
    target_record = _find_odoo_instance_override_record(
        existing_records=existing_records,
        context_name=normalized_context,
        instance_name=normalized_instance,
    )
    config_parameters = target_record.config_parameters if target_record is not None else ()
    addon_settings = {
        (override.addon, override.setting): override
        for override in (target_record.addon_settings if target_record is not None else ())
    }
    addon_settings[(normalized_addon, normalized_setting)] = OdooAddonSettingOverride(
        addon=normalized_addon,
        setting=normalized_setting,
        value=override_value,
    )
    apply_on = target_record.apply_on if target_record is not None else ()
    apply_phases = tuple(dict.fromkeys((*apply_on, "deploy", "promotion")))
    return OdooInstanceOverrideRecord(
        context=normalized_context,
        instance=normalized_instance,
        apply_on=apply_phases,
        config_parameters=config_parameters,
        addon_settings=tuple(addon_settings[key] for key in sorted(addon_settings)),
        website_bootstrap=target_record.website_bootstrap if target_record is not None else None,
        updated_at=utc_now_timestamp(),
        source_label=source_label.strip() or "cli",
    )


def _build_odoo_instance_override_record_with_website_bootstrap(
    *,
    existing_records: tuple[OdooInstanceOverrideRecord, ...],
    context_name: str,
    instance_name: str,
    website_bootstrap: OdooWebsiteBootstrapPayload,
    source_label: str,
) -> OdooInstanceOverrideRecord:
    normalized_context = context_name.strip().lower()
    normalized_instance = instance_name.strip().lower()
    if not normalized_context or not normalized_instance:
        raise click.ClickException(
            "Odoo instance override records require --context and --instance."
        )
    target_record = _find_odoo_instance_override_record(
        existing_records=existing_records,
        context_name=normalized_context,
        instance_name=normalized_instance,
    )
    apply_on = target_record.apply_on if target_record is not None else ()
    apply_phases = tuple(dict.fromkeys((*apply_on, "deploy", "promotion")))
    return OdooInstanceOverrideRecord(
        context=normalized_context,
        instance=normalized_instance,
        apply_on=apply_phases,
        config_parameters=target_record.config_parameters if target_record is not None else (),
        addon_settings=target_record.addon_settings if target_record is not None else (),
        website_bootstrap=website_bootstrap,
        updated_at=utc_now_timestamp(),
        source_label=source_label.strip() or "cli",
    )


def _migrate_odoo_override_secret_transport_record(
    *,
    record_store: PostgresRecordStore,
    record: OdooInstanceOverrideRecord,
    apply_changes: bool,
    source_label: str,
) -> tuple[OdooInstanceOverrideRecord, list[dict[str, str]]]:
    binding_by_id = {binding.binding_id: binding for binding in record_store.list_secret_bindings()}
    changes: list[dict[str, str]] = []
    config_parameters: list[OdooConfigParameterOverride] = []
    addon_settings: list[OdooAddonSettingOverride] = []

    for override in record.config_parameters:
        value = override.value
        if value.source != "secret_binding":
            config_parameters.append(override)
            continue
        desired_binding_key = control_plane_odoo_instance_overrides.config_parameter_secret_env_key(
            override.key
        )
        binding = binding_by_id.get(value.secret_binding_id)
        if binding is None:
            raise click.ClickException(
                f"Odoo override {record.context}/{record.instance} config parameter {override.key!r} "
                f"references missing secret binding {value.secret_binding_id!r}."
            )
        desired_binding_id, action = _relabel_odoo_override_secret_binding(
            record_store=record_store,
            binding=binding,
            desired_binding_key=desired_binding_key,
            apply_changes=apply_changes,
            source_label=source_label,
        )
        changes.append(
            {
                "kind": "config_parameter",
                "key": override.key,
                "action": action,
                "old_binding_id": value.secret_binding_id,
                "new_binding_id": desired_binding_id,
                "old_binding_key": binding.binding_key,
                "new_binding_key": desired_binding_key,
            }
        )
        config_parameters.append(
            OdooConfigParameterOverride(
                key=override.key,
                value=OdooOverrideValue(
                    source="secret_binding", secret_binding_id=desired_binding_id
                ),
            )
        )

    for addon_override in record.addon_settings:
        value = addon_override.value
        if value.source != "secret_binding":
            addon_settings.append(addon_override)
            continue
        desired_binding_key = control_plane_odoo_instance_overrides.addon_setting_secret_env_key(
            addon_name=addon_override.addon, setting_name=addon_override.setting
        )
        binding = binding_by_id.get(value.secret_binding_id)
        if binding is None:
            raise click.ClickException(
                f"Odoo override {record.context}/{record.instance} addon setting "
                f"{addon_override.addon}.{addon_override.setting} references missing secret binding "
                f"{value.secret_binding_id!r}."
            )
        desired_binding_id, action = _relabel_odoo_override_secret_binding(
            record_store=record_store,
            binding=binding,
            desired_binding_key=desired_binding_key,
            apply_changes=apply_changes,
            source_label=source_label,
        )
        changes.append(
            {
                "kind": "addon_setting",
                "key": f"{addon_override.addon}.{addon_override.setting}",
                "action": action,
                "old_binding_id": value.secret_binding_id,
                "new_binding_id": desired_binding_id,
                "old_binding_key": binding.binding_key,
                "new_binding_key": desired_binding_key,
            }
        )
        addon_settings.append(
            OdooAddonSettingOverride(
                addon=addon_override.addon,
                setting=addon_override.setting,
                value=OdooOverrideValue(
                    source="secret_binding", secret_binding_id=desired_binding_id
                ),
            )
        )

    updated_record = record
    if apply_changes and any(
        change["old_binding_id"] != change["new_binding_id"] for change in changes
    ):
        updated_record = OdooInstanceOverrideRecord(
            context=record.context,
            instance=record.instance,
            apply_on=record.apply_on,
            config_parameters=tuple(config_parameters),
            addon_settings=tuple(addon_settings),
            website_bootstrap=record.website_bootstrap,
            last_apply=record.last_apply,
            updated_at=utc_now_timestamp(),
            source_label=source_label.strip() or "odoo-secret-transport-migration",
        )
        record_store.write_odoo_instance_override_record(updated_record)
    return updated_record, changes


def _build_odoo_instance_override_record_with_apply_result(
    *,
    existing_records: tuple[OdooInstanceOverrideRecord, ...],
    context_name: str,
    instance_name: str,
    status: str,
    detail: str,
    applied_at: str,
    source_label: str,
) -> OdooInstanceOverrideRecord:
    normalized_context = context_name.strip().lower()
    normalized_instance = instance_name.strip().lower()
    if not normalized_context or not normalized_instance:
        raise click.ClickException(
            "Odoo instance override records require --context and --instance."
        )
    target_record = _find_odoo_instance_override_record(
        existing_records=existing_records,
        context_name=normalized_context,
        instance_name=normalized_instance,
    )
    if target_record is None:
        raise click.ClickException(
            "Missing DB-backed Odoo instance override record for the requested instance."
        )
    normalized_status = normalize_odoo_apply_status(status)
    attempted = normalized_status in {"pending", "pass", "fail"}
    return target_record.model_copy(
        update={
            "last_apply": OdooOverrideApplyResult(
                attempted=attempted,
                status=normalized_status,
                applied_at=applied_at.strip()
                or (utc_now_timestamp() if normalized_status in {"pass", "fail"} else ""),
                detail=detail,
            ),
            "updated_at": utc_now_timestamp(),
            "source_label": source_label.strip() or "cli",
        }
    )


@click.group("odoo-overrides")
def odoo_overrides() -> None:
    """Odoo instance override record commands."""


@click.group("odoo-targets")
def odoo_targets() -> None:
    """Odoo stable target planning commands."""


@click.group("odoo-ownership")
def odoo_ownership() -> None:
    """Odoo ownership-boundary regression checks."""


@odoo_ownership.command("check")
@click.option(
    "--workspace-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Directory containing launchplane and sibling Odoo repositories.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
)
def odoo_ownership_check(workspace_root: Path | None, output_format: str) -> None:
    resolved_workspace_root = workspace_root or _control_plane_root().parent
    result = scan_odoo_ownership_boundaries(workspace_root=resolved_workspace_root)
    if output_format == "json":
        click.echo(render_odoo_ownership_scan_json(result))
    else:
        click.echo(render_odoo_ownership_scan_text(result))
    if result.status != "pass":
        raise click.ClickException("Odoo ownership boundary check failed.")


@odoo_targets.command("replacement-plan")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane Odoo target records.",
)
@click.option("--product", default="odoo-tenant-cm", show_default=True)
@click.option("--instance", "instance_name", required=True)
@click.option(
    "--strategy",
    type=click.Choice(["recreate-in-place", "replace-and-cutover"]),
    default="recreate-in-place",
    show_default=True,
)
@click.option(
    "--allow-empty-data",
    is_flag=True,
    help="Allow missing volume env keys in the dry-run plan for an intentionally empty target.",
)
@click.option(
    "--data-source-mode",
    type=click.Choice(["existing", "empty", "upstream_restore"]),
    default="existing",
    show_default=True,
    help="Prelaunch rebuild data authority for intentionally resettable targets.",
)
@click.option(
    "--confirmation",
    default="",
    help="Confirmation phrase required by the lane prelaunch rebuild policy.",
)
@click.option(
    "--control-plane-root",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Optional Launchplane repo root used to resolve Dokploy credentials.",
)
def odoo_targets_replacement_plan(
    database_url: str,
    product: str,
    instance_name: str,
    strategy: str,
    allow_empty_data: bool,
    data_source_mode: str,
    confirmation: str,
    control_plane_root: Path | None,
) -> None:
    launchplane_root = control_plane_root or _control_plane_root()
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        plan = build_odoo_stable_target_replacement_plan(
            control_plane_root=launchplane_root,
            record_store=postgres_store,
            request=OdooStableTargetReplacementRequest(
                product=product,
                instance=instance_name,
                strategy=cast(Literal["recreate-in-place", "replace-and-cutover"], strategy),
                allow_empty_data=allow_empty_data,
                data_source_mode=cast(
                    Literal["existing", "empty", "upstream_restore"], data_source_mode
                ),
                confirmation=confirmation,
            ),
            dokploy_config_reader=partial(_read_dokploy_config, database_url=database_url),
        )
    finally:
        postgres_store.close()
    click.echo(json.dumps(plan.model_dump(mode="json"), indent=2, sort_keys=True))


@odoo_overrides.command("put-config-param")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane Odoo override records.",
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option("--key", "key_name", required=True, help="Odoo ir.config_parameter key.")
@click.option("--value", default=None, help="Non-secret literal value.")
@click.option(
    "--secret-binding-id", default="", help="Managed secret binding id for secret values."
)
@click.option("--source-label", default="cli", show_default=True)
@_direct_db_mutation_acknowledgement_option
def odoo_overrides_put_config_param(
    database_url: str,
    context_name: str,
    instance_name: str,
    key_name: str,
    value: str | None,
    secret_binding_id: str,
    source_label: str,
    allow_direct_db_mutation: bool,
) -> None:
    _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)
    override_value = _build_odoo_override_value(
        value=value,
        secret_binding_id=secret_binding_id,
        value_name=key_name,
    )
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        existing_records = postgres_store.list_odoo_instance_override_records()
        record = _build_odoo_instance_override_record_with_config_parameter(
            existing_records=existing_records,
            context_name=context_name,
            instance_name=instance_name,
            key_name=key_name,
            override_value=override_value,
            source_label=source_label,
        )
        postgres_store.write_odoo_instance_override_record(record)
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {"status": "ok", "record": _summarize_odoo_instance_override_record(record)},
            indent=2,
            sort_keys=True,
        )
    )


@odoo_overrides.command("put-addon-setting")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane Odoo override records.",
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option("--addon", "addon_name", required=True, help="Odoo addon or integration name.")
@click.option("--setting", "setting_name", required=True, help="Addon setting name.")
@click.option("--value", default=None, help="Non-secret literal value.")
@click.option(
    "--secret-binding-id", default="", help="Managed secret binding id for secret values."
)
@click.option("--source-label", default="cli", show_default=True)
@_direct_db_mutation_acknowledgement_option
def odoo_overrides_put_addon_setting(
    database_url: str,
    context_name: str,
    instance_name: str,
    addon_name: str,
    setting_name: str,
    value: str | None,
    secret_binding_id: str,
    source_label: str,
    allow_direct_db_mutation: bool,
) -> None:
    _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)
    value_name = f"{addon_name}.{setting_name}"
    override_value = _build_odoo_override_value(
        value=value,
        secret_binding_id=secret_binding_id,
        value_name=value_name,
    )
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        existing_records = postgres_store.list_odoo_instance_override_records()
        record = _build_odoo_instance_override_record_with_addon_setting(
            existing_records=existing_records,
            context_name=context_name,
            instance_name=instance_name,
            addon_name=addon_name,
            setting_name=setting_name,
            override_value=override_value,
            source_label=source_label,
        )
        postgres_store.write_odoo_instance_override_record(record)
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {"status": "ok", "record": _summarize_odoo_instance_override_record(record)},
            indent=2,
            sort_keys=True,
        )
    )


@odoo_overrides.command("put-website-bootstrap")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane Odoo override records.",
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option(
    "--payload-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    required=True,
    help="JSON file containing a typed devkit website_bootstrap payload.",
)
@click.option("--source-label", default="cli", show_default=True)
@_direct_db_mutation_acknowledgement_option
def odoo_overrides_put_website_bootstrap(
    database_url: str,
    context_name: str,
    instance_name: str,
    payload_file: Path,
    source_label: str,
    allow_direct_db_mutation: bool,
) -> None:
    _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)
    try:
        raw_payload = json.loads(payload_file.read_text(encoding="utf-8"))
        website_bootstrap = validate_odoo_website_bootstrap_contract(
            OdooWebsiteBootstrapPayload.model_validate(raw_payload)
        )
    except JSONDecodeError as error:
        raise click.ClickException("Odoo website bootstrap payload must be valid JSON.") from error
    except (ValidationError, ValueError) as error:
        raise click.ClickException(f"Invalid Odoo website bootstrap payload: {error}") from error
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        existing_records = postgres_store.list_odoo_instance_override_records()
        record = _build_odoo_instance_override_record_with_website_bootstrap(
            existing_records=existing_records,
            context_name=context_name,
            instance_name=instance_name,
            website_bootstrap=website_bootstrap,
            source_label=source_label,
        )
        postgres_store.write_odoo_instance_override_record(record)
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {"status": "ok", "record": _summarize_odoo_instance_override_record(record)},
            indent=2,
            sort_keys=True,
        )
    )


@odoo_overrides.command("migrate-secret-transport")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane Odoo override records.",
)
@click.option("--context", "context_name", default="")
@click.option("--instance", "instance_name", default="")
@click.option("--apply", "apply_changes", is_flag=True, help="Persist binding relabels.")
@click.option(
    "--source-label",
    default="odoo-secret-transport-migration",
    show_default=True,
)
@_direct_db_mutation_acknowledgement_option
def odoo_overrides_migrate_secret_transport(
    database_url: str,
    context_name: str,
    instance_name: str,
    apply_changes: bool,
    source_label: str,
    allow_direct_db_mutation: bool,
) -> None:
    normalized_context = context_name.strip().lower()
    normalized_instance = instance_name.strip().lower()
    if normalized_instance and not normalized_context:
        raise click.ClickException("--instance requires --context.")
    if apply_changes:
        _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)

    postgres_store = PostgresRecordStore(database_url=database_url)
    if apply_changes:
        postgres_store.ensure_schema()
    try:
        records = postgres_store.list_odoo_instance_override_records()
        selected_records = [
            record
            for record in records
            if (not normalized_context or record.context == normalized_context)
            and (not normalized_instance or record.instance == normalized_instance)
        ]
        migrations: list[dict[str, object]] = []
        changed_records = 0
        for record in selected_records:
            updated_record, changes = _migrate_odoo_override_secret_transport_record(
                record_store=postgres_store,
                record=record,
                apply_changes=apply_changes,
                source_label=source_label,
            )
            changed = any(
                change["old_binding_id"] != change["new_binding_id"] for change in changes
            )
            if apply_changes and changed:
                changed_records += 1
            migrations.append(
                {
                    "context": record.context,
                    "instance": record.instance,
                    "changed": changed,
                    "record_updated_at": updated_record.updated_at,
                    "secret_overrides": changes,
                }
            )
    finally:
        postgres_store.close()

    click.echo(
        json.dumps(
            {
                "status": "ok",
                "mode": "apply" if apply_changes else "dry-run",
                "record_count": len(selected_records),
                "changed_record_count": changed_records,
                "migrations": migrations,
            },
            indent=2,
            sort_keys=True,
        )
    )


@odoo_overrides.command("list")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane Odoo override records.",
)
def odoo_overrides_list(database_url: str) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        records = postgres_store.list_odoo_instance_override_records()
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {
                "status": "ok",
                "count": len(records),
                "records": [_summarize_odoo_instance_override_record(record) for record in records],
            },
            indent=2,
            sort_keys=True,
        )
    )


@odoo_overrides.command("show")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane Odoo override records.",
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
def odoo_overrides_show(database_url: str, context_name: str, instance_name: str) -> None:
    postgres_store = PostgresRecordStore(database_url=database_url)
    try:
        record = postgres_store.read_odoo_instance_override_record(
            context_name=context_name.strip().lower(),
            instance_name=instance_name.strip().lower(),
        )
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(_summarize_odoo_instance_override_record(record), indent=2, sort_keys=True)
    )


@odoo_overrides.command("mark-apply")
@click.option(
    "--database-url",
    envvar=_DATABASE_URL_ENV_KEYS,
    required=True,
    help="Postgres connection string for Launchplane Odoo override records.",
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option("--status", type=click.Choice(["skipped", "pending", "pass", "fail"]), required=True)
@click.option("--applied-at", default="", help="UTC timestamp for completed apply results.")
@click.option("--detail", default="")
@click.option("--source-label", default="cli", show_default=True)
@_direct_db_mutation_acknowledgement_option
def odoo_overrides_mark_apply(
    database_url: str,
    context_name: str,
    instance_name: str,
    status: str,
    applied_at: str,
    detail: str,
    source_label: str,
    allow_direct_db_mutation: bool,
) -> None:
    _require_direct_db_mutation_acknowledgement(allow_direct_db_mutation)
    postgres_store = PostgresRecordStore(database_url=database_url)
    postgres_store.ensure_schema()
    try:
        existing_records = postgres_store.list_odoo_instance_override_records()
        record = _build_odoo_instance_override_record_with_apply_result(
            existing_records=existing_records,
            context_name=context_name,
            instance_name=instance_name,
            status=status,
            detail=detail,
            applied_at=applied_at,
            source_label=source_label,
        )
        postgres_store.write_odoo_instance_override_record(record)
    finally:
        postgres_store.close()
    click.echo(
        json.dumps(
            {"status": "ok", "record": _summarize_odoo_instance_override_record(record)},
            indent=2,
            sort_keys=True,
        )
    )
