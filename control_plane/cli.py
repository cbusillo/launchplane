import json
import os
import subprocess
from pathlib import Path

import click

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.promotion_record import CompatibilityPromotionRequest, PromotionRecord
from control_plane.contracts.ship_request import CompatibilityShipRequest
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.promote import (
    build_compatibility_promotion_record,
    build_promotion_record,
    generate_promotion_record_id,
)


def _store(state_dir: Path) -> FilesystemRecordStore:
    return FilesystemRecordStore(state_dir=state_dir)


def _load_json_file(input_file: Path) -> dict[str, object]:
    return json.loads(input_file.read_text(encoding="utf-8"))


def _run_command(command: list[str]) -> None:
    command_env = dict(os.environ)
    command_env.pop("VIRTUAL_ENV", None)
    subprocess.run(command, check=True, env=command_env)


@click.group()
def main() -> None:
    """Control-plane CLI."""


@main.group()
def artifacts() -> None:
    """Artifact manifest commands."""


@artifacts.command("write")
@click.option("--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def artifacts_write(state_dir: Path, input_file: Path) -> None:
    manifest = ArtifactIdentityManifest.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_artifact_manifest(manifest)
    click.echo(record_path)


@artifacts.command("show")
@click.option("--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True)
@click.option("--artifact-id", required=True)
def artifacts_show(state_dir: Path, artifact_id: str) -> None:
    manifest = _store(state_dir).read_artifact_manifest(artifact_id)
    click.echo(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))


@main.group()
def promotions() -> None:
    """Promotion record commands."""


@promotions.command("write")
@click.option("--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def promotions_write(state_dir: Path, input_file: Path) -> None:
    record = PromotionRecord.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_promotion_record(record)
    click.echo(record_path)


@promotions.command("show")
@click.option("--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True)
@click.option("--record-id", required=True)
def promotions_show(state_dir: Path, record_id: str) -> None:
    record = _store(state_dir).read_promotion_record(record_id)
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@main.group()
def promote() -> None:
    """Promotion workflow commands."""


@promote.command("record")
@click.option("--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True)
@click.option("--record-id", required=True)
@click.option("--artifact-id", required=True)
@click.option("--context", "context_name", required=True)
@click.option("--from-instance", "from_instance_name", required=True)
@click.option("--to-instance", "to_instance_name", required=True)
@click.option("--target-name", required=True)
@click.option("--target-type", type=click.Choice(["compose", "application"]), required=True)
@click.option("--deploy-mode", required=True)
@click.option("--deployment-id", default="", show_default=False)
def promote_record(
    state_dir: Path,
    record_id: str,
    artifact_id: str,
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
        context_name=context_name,
        from_instance_name=from_instance_name,
        to_instance_name=to_instance_name,
        target_name=target_name,
        target_type=target_type,
        deploy_mode=deploy_mode,
        deployment_id=deployment_id,
    )
    record_path = _store(state_dir).write_promotion_record(record)
    click.echo(record_path)


@promote.command("compatibility-execute")
@click.option("--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--odoo-ai-root", type=click.Path(exists=True, file_okay=False, path_type=Path), required=True)
@click.option("--env-file", type=click.Path(exists=True, path_type=Path), default=None)
def promote_compatibility_execute(
    state_dir: Path,
    input_file: Path,
    odoo_ai_root: Path,
    env_file: Path | None,
) -> None:
    request = CompatibilityPromotionRequest.model_validate(_load_json_file(input_file))
    record_id = generate_promotion_record_id(
        context_name=request.context,
        from_instance_name=request.from_instance,
        to_instance_name=request.to_instance,
    )
    if request.dry_run:
        click.echo(
            json.dumps(
                build_compatibility_promotion_record(
                    request=request,
                    record_id=record_id,
                    deployment_id="",
                    deployment_status="pending",
                ).model_dump(mode="json"),
                indent=2,
                sort_keys=True,
            )
        )
        return

    pending_record = build_compatibility_promotion_record(
        request=request,
        record_id=record_id,
        deployment_id="",
        deployment_status="pending",
    )
    record_store = _store(state_dir)
    record_path = record_store.write_promotion_record(pending_record)

    ship_command = [
        "uv",
        "run",
        "--project",
        str(odoo_ai_root),
        "platform",
        "ship",
        "--context",
        request.context,
        "--instance",
        request.to_instance,
        "--source-ref",
        request.source_git_ref,
        "--skip-gate",
    ]
    if env_file is not None:
        ship_command.extend(["--env-file", str(env_file)])
    if request.wait:
        ship_command.append("--wait")
    else:
        ship_command.append("--no-wait")
    if request.timeout_seconds is not None:
        ship_command.extend(["--timeout", str(request.timeout_seconds)])
    if request.verify_health:
        ship_command.append("--verify-health")
    else:
        ship_command.append("--no-verify-health")
    if request.health_timeout_seconds is not None:
        ship_command.extend(["--health-timeout", str(request.health_timeout_seconds)])
    if request.no_cache:
        ship_command.append("--no-cache")
    if request.allow_dirty:
        ship_command.append("--allow-dirty")

    try:
        _run_command(ship_command)
        final_record = build_compatibility_promotion_record(
            request=request,
            record_id=record_id,
            deployment_id="delegated-odoo-ai-ship",
            deployment_status="pass",
        )
    except subprocess.CalledProcessError:
        final_record = build_compatibility_promotion_record(
            request=request,
            record_id=record_id,
            deployment_id="delegated-odoo-ai-ship",
            deployment_status="fail",
        )
        record_store.write_promotion_record(final_record)
        raise

    record_store.write_promotion_record(final_record)
    click.echo(record_path)


@main.group()
def ship() -> None:
    """Ship workflow commands."""


@ship.command("compatibility-plan")
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def ship_compatibility_plan(input_file: Path) -> None:
    request = CompatibilityShipRequest.model_validate(_load_json_file(input_file))
    click.echo(json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True))
