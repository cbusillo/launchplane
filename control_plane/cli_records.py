import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import click

from control_plane import release_tuples as control_plane_release_tuples
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.protected_artifacts import build_protected_artifact_set
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore


_DATABASE_URL_ENV_KEYS = ("LAUNCHPLANE_DATABASE_URL",)
_LOCAL_REHEARSAL_ONLY_MESSAGE = (
    "Core-record write commands are local-rehearsal only after the Launchplane "
    "service boundary. Use the deployed service route or operator workflow for "
    "shared/production evidence ingress."
)


@dataclass(frozen=True)
class RecordsCliCallbacks:
    store_factory: Callable[..., FilesystemRecordStore | PostgresRecordStore]
    load_json_file: Callable[[Path], dict[str, object]]
    summarize_backup_gate_record: Callable[[BackupGateRecord], dict[str, object]]
    summarize_promotion_record: Callable[[PromotionRecord], dict[str, object]]
    summarize_deployment_record: Callable[[DeploymentRecord], dict[str, object]]
    write_environment_inventory: Callable[..., Path | None]
    write_environment_inventory_from_promotion: Callable[..., Path | None]
    read_source_release_tuple_for_promotion_record: Callable[..., ReleaseTupleRecord]
    write_promoted_release_tuple: Callable[..., Path | None]
    build_environment_status_payload: Callable[..., dict[str, object]]
    build_environment_overview_payloads: Callable[..., list[dict[str, object]]]
    summarize_environment_inventory: Callable[[EnvironmentInventory], dict[str, object]]


_callbacks: RecordsCliCallbacks | None = None


def register_record_commands(main: click.Group, *, callbacks: RecordsCliCallbacks) -> None:
    global _callbacks
    _callbacks = callbacks
    main.add_command(artifacts)
    main.add_command(release_tuples)
    main.add_command(backup_gates)
    main.add_command(promotions)
    main.add_command(deployments)
    main.add_command(inventory)


def _records_callbacks() -> RecordsCliCallbacks:
    if _callbacks is None:
        raise click.ClickException("Launchplane records CLI callbacks are not configured.")
    return _callbacks


def _store(
    state_dir: Path, *, database_url: str | None = None
) -> FilesystemRecordStore | PostgresRecordStore:
    return _records_callbacks().store_factory(state_dir, database_url=database_url)


def _resolve_record_execution_database_url(
    *,
    database_url: str,
    local_rehearsal: bool,
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
    return normalized_database_url


def _require_local_rehearsal(local_rehearsal: bool) -> None:
    if not local_rehearsal:
        raise click.ClickException(_LOCAL_REHEARSAL_ONLY_MESSAGE)


def _load_json_file(input_file: Path) -> dict[str, object]:
    return _records_callbacks().load_json_file(input_file)


@click.group()
def artifacts() -> None:
    """Artifact manifest commands."""


@artifacts.command("write")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--local-rehearsal", is_flag=True, default=False)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def artifacts_write(
    state_dir: Path,
    local_rehearsal: bool,
    input_file: Path,
) -> None:
    _write_artifact_manifest_command(
        state_dir=state_dir,
        local_rehearsal=local_rehearsal,
        input_file=input_file,
    )


@artifacts.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--artifact-id", required=True)
def artifacts_show(state_dir: Path, database_url: str, artifact_id: str) -> None:
    manifest = _store(state_dir, database_url=database_url).read_artifact_manifest(artifact_id)
    click.echo(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))


@artifacts.command("protected")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--local-rehearsal", is_flag=True, default=False)
@click.option("--product", "product_name", default="", show_default=False)
@click.option("--context", "context_name", default="", show_default=False)
def artifacts_protected(
    state_dir: Path,
    database_url: str,
    local_rehearsal: bool,
    product_name: str,
    context_name: str,
) -> None:
    execution_database_url = _resolve_record_execution_database_url(
        database_url=database_url,
        local_rehearsal=local_rehearsal,
        command_label="artifacts protected",
    )
    protected = build_protected_artifact_set(
        _store(state_dir, database_url=execution_database_url),
        product=product_name,
        context_name=context_name,
    )
    click.echo(json.dumps(protected.model_dump(mode="json"), indent=2, sort_keys=True))


def _write_artifact_manifest_command(
    *,
    state_dir: Path,
    local_rehearsal: bool,
    input_file: Path,
) -> None:
    _require_local_rehearsal(local_rehearsal)
    manifest = ArtifactIdentityManifest.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_artifact_manifest(manifest)
    click.echo(record_path)


@click.group("release-tuples")
def release_tuples() -> None:
    """Release tuple state commands."""


@release_tuples.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
def release_tuples_list(state_dir: Path, database_url: str) -> None:
    records = _store(state_dir, database_url=database_url).list_release_tuple_records()
    click.echo(
        json.dumps([record.model_dump(mode="json") for record in records], indent=2, sort_keys=True)
    )


@release_tuples.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--context", "context_name", required=True)
@click.option("--channel", "channel_name", required=True)
def release_tuples_show(
    state_dir: Path, database_url: str, context_name: str, channel_name: str
) -> None:
    record = _store(state_dir, database_url=database_url).read_release_tuple_record(
        context_name=context_name,
        channel_name=channel_name,
    )
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@release_tuples.command("export-catalog")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--output-file", type=click.Path(path_type=Path), default=None)
def release_tuples_export_catalog(
    state_dir: Path, database_url: str, output_file: Path | None
) -> None:
    records = _store(state_dir, database_url=database_url).list_release_tuple_records()
    if not records:
        raise click.ClickException("No release tuple records found to export.")
    rendered_catalog = control_plane_release_tuples.render_release_tuple_catalog_toml(records)
    if output_file is None:
        click.echo(rendered_catalog, nl=False)
        return
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(rendered_catalog, encoding="utf-8")
    click.echo(output_file)


@release_tuples.command("write-from-promotion")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--local-rehearsal", is_flag=True, default=False)
@click.option("--record-id", required=True)
def release_tuples_write_from_promotion(
    state_dir: Path,
    local_rehearsal: bool,
    record_id: str,
) -> None:
    _require_local_rehearsal(local_rehearsal)
    record_store = _store(state_dir)
    promotion_record = record_store.read_promotion_record(record_id)
    if not control_plane_release_tuples.should_mint_release_tuple_for_channel(
        promotion_record.from_instance
    ) or not control_plane_release_tuples.should_mint_release_tuple_for_channel(
        promotion_record.to_instance
    ):
        raise click.ClickException(
            "Release tuple promotion only supports stable remote channels testing, prod."
        )
    deployment_record_id = promotion_record.deployment_record_id.strip()
    if not deployment_record_id:
        raise click.ClickException(
            "Promotion record is missing deployment_record_id. "
            "Write a promotion record with explicit deployment linkage before minting a promoted release tuple."
        )
    deployment_record = record_store.read_deployment_record(deployment_record_id)
    callbacks = _records_callbacks()
    source_tuple = callbacks.read_source_release_tuple_for_promotion_record(
        record_store=record_store,
        promotion_record=promotion_record,
    )
    tuple_path = callbacks.write_promoted_release_tuple(
        record_store=record_store,
        source_tuple=source_tuple,
        deployment_record=deployment_record,
        promotion_record=promotion_record,
    )
    if tuple_path is None:
        raise click.ClickException(
            "Promotion record did not produce a stable release tuple destination."
        )
    click.echo(tuple_path)


@click.group("backup-gates")
def backup_gates() -> None:
    """Backup gate record commands."""


@backup_gates.command("write")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--local-rehearsal", is_flag=True, default=False)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def backup_gates_write(
    state_dir: Path,
    local_rehearsal: bool,
    input_file: Path,
) -> None:
    _require_local_rehearsal(local_rehearsal)
    record = BackupGateRecord.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_backup_gate_record(record)
    click.echo(record_path)


@backup_gates.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--record-id", required=True)
def backup_gates_show(state_dir: Path, database_url: str, record_id: str) -> None:
    record = _store(state_dir, database_url=database_url).read_backup_gate_record(record_id)
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@backup_gates.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--context", "context_name", default="")
@click.option("--instance", "instance_name", default="")
@click.option("--limit", type=click.IntRange(min=1), default=20, show_default=True)
def backup_gates_list(
    state_dir: Path, database_url: str, context_name: str, instance_name: str, limit: int
) -> None:
    records = _store(state_dir, database_url=database_url).list_backup_gate_records(
        context_name=context_name,
        instance_name=instance_name,
        limit=limit,
    )
    click.echo(
        json.dumps(
            [_records_callbacks().summarize_backup_gate_record(record) for record in records],
            indent=2,
            sort_keys=True,
        )
    )


@click.group()
def promotions() -> None:
    """Promotion record commands."""


@promotions.command("write")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--local-rehearsal", is_flag=True, default=False)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def promotions_write(
    state_dir: Path,
    local_rehearsal: bool,
    input_file: Path,
) -> None:
    _require_local_rehearsal(local_rehearsal)
    record = PromotionRecord.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_promotion_record(record)
    click.echo(record_path)


@promotions.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--record-id", required=True)
def promotions_show(state_dir: Path, database_url: str, record_id: str) -> None:
    record = _store(state_dir, database_url=database_url).read_promotion_record(record_id)
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@promotions.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--context", "context_name", default="")
@click.option("--from-instance", "from_instance_name", default="")
@click.option("--to-instance", "to_instance_name", default="")
@click.option("--limit", type=click.IntRange(min=1), default=20, show_default=True)
def promotions_list(
    state_dir: Path,
    database_url: str,
    context_name: str,
    from_instance_name: str,
    to_instance_name: str,
    limit: int,
) -> None:
    records = _store(state_dir, database_url=database_url).list_promotion_records(
        context_name=context_name,
        from_instance_name=from_instance_name,
        to_instance_name=to_instance_name,
        limit=limit,
    )
    click.echo(
        json.dumps(
            [_records_callbacks().summarize_promotion_record(record) for record in records],
            indent=2,
            sort_keys=True,
        )
    )


@click.group()
def deployments() -> None:
    """Deployment record commands."""


@deployments.command("write")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--local-rehearsal", is_flag=True, default=False)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def deployments_write(
    state_dir: Path,
    local_rehearsal: bool,
    input_file: Path,
) -> None:
    _require_local_rehearsal(local_rehearsal)
    record = DeploymentRecord.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_deployment_record(record)
    click.echo(record_path)


@deployments.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--record-id", required=True)
def deployments_show(state_dir: Path, database_url: str, record_id: str) -> None:
    record = _store(state_dir, database_url=database_url).read_deployment_record(record_id)
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@deployments.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--context", "context_name", default="")
@click.option("--instance", "instance_name", default="")
@click.option("--limit", type=click.IntRange(min=1), default=20, show_default=True)
def deployments_list(
    state_dir: Path, database_url: str, context_name: str, instance_name: str, limit: int
) -> None:
    records = _store(state_dir, database_url=database_url).list_deployment_records(
        context_name=context_name,
        instance_name=instance_name,
        limit=limit,
    )
    click.echo(
        json.dumps(
            [_records_callbacks().summarize_deployment_record(record) for record in records],
            indent=2,
            sort_keys=True,
        )
    )


@click.group()
def inventory() -> None:
    """Environment inventory commands."""


@inventory.command("write-from-deployment")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--local-rehearsal", is_flag=True, default=False)
@click.option("--record-id", required=True)
def inventory_write_from_deployment(
    state_dir: Path,
    local_rehearsal: bool,
    record_id: str,
) -> None:
    _require_local_rehearsal(local_rehearsal)
    record_store = _store(state_dir)
    deployment_record = record_store.read_deployment_record(record_id)
    inventory_path = _records_callbacks().write_environment_inventory(
        record_store=record_store,
        deployment_record=deployment_record,
    )
    click.echo(inventory_path)


@inventory.command("write-from-promotion")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--local-rehearsal", is_flag=True, default=False)
@click.option("--record-id", required=True)
def inventory_write_from_promotion(
    state_dir: Path,
    local_rehearsal: bool,
    record_id: str,
) -> None:
    _require_local_rehearsal(local_rehearsal)
    record_store = _store(state_dir)
    promotion_record = record_store.read_promotion_record(record_id)
    inventory_path = _records_callbacks().write_environment_inventory_from_promotion(
        record_store=record_store,
        promotion_record=promotion_record,
    )
    click.echo(inventory_path)


@inventory.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
def inventory_show(
    state_dir: Path, database_url: str, context_name: str, instance_name: str
) -> None:
    record = _store(state_dir, database_url=database_url).read_environment_inventory(
        context_name=context_name, instance_name=instance_name
    )
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@inventory.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
def inventory_list(state_dir: Path, database_url: str) -> None:
    records = _store(state_dir, database_url=database_url).list_environment_inventory()
    click.echo(
        json.dumps([record.model_dump(mode="json") for record in records], indent=2, sort_keys=True)
    )


@inventory.command("overview")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--context", "context_name", default="")
def inventory_overview(state_dir: Path, database_url: str, context_name: str) -> None:
    callbacks = _records_callbacks()
    payload = callbacks.build_environment_overview_payloads(
        record_store=_store(state_dir, database_url=database_url),
        context_name=context_name,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@inventory.command("status")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--database-url", default="", show_default=False)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
def inventory_status(
    state_dir: Path, database_url: str, context_name: str, instance_name: str
) -> None:
    callbacks = _records_callbacks()
    payload = callbacks.build_environment_status_payload(
        record_store=_store(state_dir, database_url=database_url),
        context_name=context_name,
        instance_name=instance_name,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))
