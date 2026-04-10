import json
from pathlib import Path

import click

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.promote import build_promotion_record


def _store(state_dir: Path) -> FilesystemRecordStore:
    return FilesystemRecordStore(state_dir=state_dir)


def _load_json_file(input_file: Path) -> dict[str, object]:
    return json.loads(input_file.read_text(encoding="utf-8"))


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
