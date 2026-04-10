import json
import os
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.promotion_record import CompatibilityPromotionRequest, HealthcheckEvidence, PostDeployUpdateEvidence, PromotionRecord
from control_plane.contracts.ship_request import ShipRequest
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.promote import (
    build_compatibility_promotion_record,
    build_promotion_record,
    generate_promotion_record_id,
)
from control_plane.workflows.ship import (
    build_deployment_record,
    generate_deployment_record_id,
    utc_now_timestamp,
)

ARTIFACT_IMAGE_REFERENCE_ENV_KEY = "DOCKER_IMAGE_REFERENCE"


def _store(state_dir: Path) -> FilesystemRecordStore:
    return FilesystemRecordStore(state_dir=state_dir)


def _load_json_file(input_file: Path) -> dict[str, object]:
    return json.loads(input_file.read_text(encoding="utf-8"))


def _run_command(command: list[str], *, cwd: Path | None = None) -> None:
    command_env = dict(os.environ)
    command_env.pop("VIRTUAL_ENV", None)
    subprocess.run(command, check=True, env=command_env, cwd=cwd)


def _run_command_capture(command: list[str], *, cwd: Path | None = None) -> str:
    command_env = dict(os.environ)
    command_env.pop("VIRTUAL_ENV", None)
    result = subprocess.run(command, check=True, env=command_env, cwd=cwd, capture_output=True, text=True)
    return result.stdout


def _wait_for_ship_healthcheck(*, url: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: str = ""
    while time.monotonic() < deadline:
        try:
            request = Request(url, method="GET")
            with urlopen(request, timeout=min(5, timeout_seconds)) as response:
                if 200 <= response.status < 300:
                    return
                last_error = f"http {response.status}"
        except HTTPError as error:
            last_error = f"http {error.code}"
        except URLError as error:
            last_error = str(error.reason)
        time.sleep(1)
    raise click.ClickException(f"Healthcheck failed for {url}: {last_error or 'timeout'}")


def _verify_ship_healthchecks(*, request: ShipRequest) -> None:
    if not request.wait or not request.verify_health:
        return
    if not request.destination_health.urls:
        raise click.ClickException(
            "Healthcheck verification requested but no target domain/URL was resolved. "
            "Define domains in platform/dokploy.toml or disable with --no-verify-health."
        )
    if request.destination_health.timeout_seconds is None:
        raise click.ClickException("Healthcheck verification requested without timeout_seconds.")
    for healthcheck_url in request.destination_health.urls:
        _wait_for_ship_healthcheck(url=healthcheck_url, timeout_seconds=request.destination_health.timeout_seconds)


def _resolve_dokploy_target(
    *,
    odoo_ai_root: Path,
    request: ShipRequest,
) -> tuple[ResolvedTargetEvidence, int]:
    source_of_truth = control_plane_dokploy.load_dokploy_source_of_truth(odoo_ai_root / "platform" / "dokploy.toml")
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=request.context,
        instance_name=request.instance,
    )
    if target_definition is None:
        raise click.ClickException(f"No Dokploy target definition found for {request.context}/{request.instance}.")
    if target_definition.target_type != request.target_type:
        raise click.ClickException(
            "Ship request target_type does not match platform/dokploy.toml. "
            f"Request={request.target_type} configured={target_definition.target_type}."
        )
    if not target_definition.target_id.strip():
        raise click.ClickException(
            f"Dokploy target {request.context}/{request.instance} is missing target_id in platform/dokploy.toml."
        )
    resolved_target = ResolvedTargetEvidence(
        target_type=target_definition.target_type,
        target_id=target_definition.target_id,
        target_name=target_definition.target_name.strip() or request.target_name,
    )
    deploy_timeout_seconds = control_plane_dokploy.resolve_ship_timeout_seconds(
        timeout_override_seconds=request.timeout_seconds,
        target_definition=target_definition,
    )
    return resolved_target, deploy_timeout_seconds


def _execute_dokploy_deploy(
    *,
    odoo_ai_root: Path,
    env_file: Path | None,
    request: ShipRequest,
    resolved_target: ResolvedTargetEvidence,
    deploy_timeout_seconds: int,
) -> None:
    control_plane_root = Path(__file__).resolve().parent.parent
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    latest_before = None
    if request.wait:
        latest_before = control_plane_dokploy.latest_deployment_for_target(
            host=host,
            token=token,
            target_type=resolved_target.target_type,
            target_id=resolved_target.target_id,
        )
    control_plane_dokploy.trigger_deployment(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        no_cache=request.no_cache,
    )
    if not request.wait:
        return
    control_plane_dokploy.wait_for_target_deployment(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        before_key=control_plane_dokploy.deployment_key(latest_before),
        timeout_seconds=deploy_timeout_seconds,
    )


def _run_post_deploy_update_via_odoo_ai(
    *,
    odoo_ai_root: Path,
    env_file: Path | None,
    request: ShipRequest,
) -> None:
    command = [
        "uv",
        "run",
        "--project",
        str(odoo_ai_root),
        "platform",
        "update",
        "--context",
        request.context,
        "--instance",
        request.instance,
    ]
    if env_file is not None:
        command.extend(["--env-file", str(env_file)])
    _run_command(command)


def _skipped_destination_health(request: ShipRequest, *, detail_status: str = "skipped") -> HealthcheckEvidence:
    return request.destination_health.model_copy(update={"verified": False, "status": detail_status})


def _export_ship_request_via_odoo_ai(
    *,
    odoo_ai_root: Path,
    env_file: Path | None,
    request: CompatibilityPromotionRequest,
) -> ShipRequest:
    command = [
        "uv",
        "run",
        "--project",
        str(odoo_ai_root),
        "platform",
        "export-ship-request",
        "--context",
        request.context,
        "--instance",
        request.to_instance,
        "--artifact-id",
        request.artifact_id,
        "--source-ref",
        request.source_git_ref,
    ]
    if env_file is not None:
        command.extend(["--env-file", str(env_file)])
    if request.wait:
        command.append("--wait")
    else:
        command.append("--no-wait")
    if request.timeout_seconds is not None:
        command.extend(["--timeout", str(request.timeout_seconds)])
    if request.verify_health:
        command.append("--verify-health")
    else:
        command.append("--no-verify-health")
    if request.health_timeout_seconds is not None:
        command.extend(["--health-timeout", str(request.health_timeout_seconds)])
    if request.no_cache:
        command.append("--no-cache")
    if request.allow_dirty:
        command.append("--allow-dirty")
    payload = json.loads(_run_command_capture(command, cwd=odoo_ai_root))
    return ShipRequest.model_validate(payload)


def _execute_ship(
    *,
    state_dir: Path,
    odoo_ai_root: Path,
    env_file: Path | None,
    request: ShipRequest,
) -> tuple[Path | None, DeploymentRecord | ShipRequest]:
    record_store = _store(state_dir)
    resolved_artifact_id = _resolve_artifact_id_for_request(
        record_store=record_store,
        requested_artifact_id=request.artifact_id,
        source_git_ref=request.source_git_ref,
    )
    artifact_manifest = _read_artifact_manifest(
        record_store=record_store,
        artifact_id=resolved_artifact_id,
    )
    resolved_request = _resolve_artifact_native_execution_request(
        request=request,
        artifact_id=resolved_artifact_id,
        artifact_manifest=artifact_manifest,
    )

    if resolved_request.dry_run:
        click.echo(json.dumps(resolved_request.model_dump(mode="json"), indent=2, sort_keys=True))
        return None, resolved_request

    record_id = generate_deployment_record_id(
        context_name=resolved_request.context,
        instance_name=resolved_request.instance,
    )
    started_at = utc_now_timestamp()
    pending_record = build_deployment_record(
        request=resolved_request,
        record_id=record_id,
        deployment_id="control-plane-dokploy",
        deployment_status="pending",
        started_at=started_at,
        finished_at="",
    )
    record_path = record_store.write_deployment_record(pending_record)

    try:
        resolved_target, deploy_timeout_seconds = _resolve_dokploy_target(
            odoo_ai_root=odoo_ai_root,
            request=resolved_request,
        )
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="fail",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
        )
        record_store.write_deployment_record(final_record)
        raise

    try:
        _sync_artifact_image_reference_for_target(
            artifact_manifest=artifact_manifest,
            resolved_target=resolved_target,
        )
        _execute_dokploy_deploy(
            odoo_ai_root=odoo_ai_root,
            env_file=env_file,
            request=resolved_request,
            resolved_target=resolved_target,
            deploy_timeout_seconds=deploy_timeout_seconds,
        )
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="fail",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            resolved_target=resolved_target,
            delegated_executor="control-plane.dokploy",
        )
        record_store.write_deployment_record(final_record)
        raise

    try:
        if resolved_request.wait and resolved_target.target_type == "compose":
            _run_post_deploy_update_via_odoo_ai(
                odoo_ai_root=odoo_ai_root,
                env_file=env_file,
                request=resolved_request,
            )
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="pass",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            resolved_target=resolved_target,
            delegated_executor="control-plane.dokploy",
            post_deploy_update=PostDeployUpdateEvidence(
                attempted=True,
                status="fail",
                detail=(
                    "Odoo-specific post-deploy update failed through the canonical "
                    "odoo-ai platform update workflow."
                ),
            ),
            destination_health=_skipped_destination_health(resolved_request),
        )
        record_store.write_deployment_record(final_record)
        raise

    post_deploy_update_evidence = PostDeployUpdateEvidence()
    if resolved_request.wait and resolved_target.target_type == "compose":
        post_deploy_update_evidence = PostDeployUpdateEvidence(
            attempted=True,
            status="pass",
            detail=(
                "Odoo-specific post-deploy update completed through the canonical "
                "odoo-ai platform update workflow."
            ),
        )

    try:
        _verify_ship_healthchecks(request=resolved_request)
        final_record = build_deployment_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="pass",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            resolved_target=resolved_target,
            delegated_executor="control-plane.dokploy",
            post_deploy_update=post_deploy_update_evidence,
        )
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="pass",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            resolved_target=resolved_target,
            delegated_executor="control-plane.dokploy",
            post_deploy_update=post_deploy_update_evidence,
            destination_health=_skipped_destination_health(resolved_request, detail_status="fail"),
        )
        record_store.write_deployment_record(final_record)
        raise

    record_store.write_deployment_record(final_record)
    if final_record.wait_for_completion and final_record.deploy.status == "pass":
        _write_environment_inventory(record_store=record_store, deployment_record=final_record)
    return record_path, final_record


def _resolve_artifact_id_for_request(
    *,
    record_store: FilesystemRecordStore,
    requested_artifact_id: str,
    source_git_ref: str,
) -> str:
    matching_manifests = record_store.find_artifact_manifests_by_commit(source_git_ref)
    normalized_artifact_id = requested_artifact_id.strip()
    if len(matching_manifests) == 1:
        matched_artifact_id = matching_manifests[0].artifact_id
        if not normalized_artifact_id or normalized_artifact_id.startswith("compatibility-"):
            return matched_artifact_id
        return normalized_artifact_id
    if normalized_artifact_id:
        return normalized_artifact_id
    if len(matching_manifests) > 1:
        raise click.ClickException(
            "Ship requires an explicit artifact_id when multiple stored artifact manifests match "
            f"source_git_ref={source_git_ref}."
        )
    raise click.ClickException(
        "Ship requires an explicit artifact_id or a unique stored artifact manifest for "
        f"source_git_ref={source_git_ref}."
    )


def _read_artifact_manifest(
    *,
    record_store: FilesystemRecordStore,
    artifact_id: str,
) -> ArtifactIdentityManifest:
    try:
        return record_store.read_artifact_manifest(artifact_id)
    except FileNotFoundError:
        raise click.ClickException(f"Ship requires stored artifact manifest '{artifact_id}'.") from None


def _resolve_artifact_native_execution_request(
    *,
    request: ShipRequest,
    artifact_id: str,
    artifact_manifest: ArtifactIdentityManifest,
) -> ShipRequest:
    if artifact_manifest.artifact_id != artifact_id:
        raise click.ClickException(
            "Artifact manifest id mismatch during ship execution: "
            f"request={artifact_id} manifest={artifact_manifest.artifact_id}."
        )
    return request.model_copy(update={"artifact_id": artifact_id})


def _artifact_image_reference_from_manifest(manifest: ArtifactIdentityManifest) -> str:
    return f"{manifest.image.repository}@{manifest.image.digest}"


def _sync_artifact_image_reference_for_target(
    *,
    artifact_manifest: ArtifactIdentityManifest | None,
    resolved_target: ResolvedTargetEvidence,
) -> None:
    control_plane_root = Path(__file__).resolve().parent.parent
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
    )
    env_map = control_plane_dokploy.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    desired_image_reference = ""
    if artifact_manifest is not None:
        desired_image_reference = _artifact_image_reference_from_manifest(artifact_manifest)

    current_image_reference = env_map.get(ARTIFACT_IMAGE_REFERENCE_ENV_KEY, "")
    if current_image_reference == desired_image_reference:
        return

    if desired_image_reference:
        env_map[ARTIFACT_IMAGE_REFERENCE_ENV_KEY] = desired_image_reference
    else:
        env_map.pop(ARTIFACT_IMAGE_REFERENCE_ENV_KEY, None)

    control_plane_dokploy.update_dokploy_target_env(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        target_payload=target_payload,
        env_text=control_plane_dokploy.serialize_dokploy_env_text(env_map),
    )


def _write_environment_inventory(
    *,
    record_store: FilesystemRecordStore,
    deployment_record: DeploymentRecord,
    promotion_record_id: str = "",
    promoted_from_instance: str = "",
) -> Path:
    inventory_record = build_environment_inventory(
        deployment_record=deployment_record,
        updated_at=utc_now_timestamp(),
        promotion_record_id=promotion_record_id,
        promoted_from_instance=promoted_from_instance,
    )
    return record_store.write_environment_inventory(inventory_record)


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


@artifacts.command("ingest-odoo-ai")
@click.option("--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def artifacts_ingest_odoo_ai(state_dir: Path, input_file: Path) -> None:
    manifest = ArtifactIdentityManifest.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_artifact_manifest(manifest)
    click.echo(record_path)


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
def inventory() -> None:
    """Environment inventory commands."""


@inventory.command("show")
@click.option("--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
def inventory_show(state_dir: Path, context_name: str, instance_name: str) -> None:
    record = _store(state_dir).read_environment_inventory(context_name=context_name, instance_name=instance_name)
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@inventory.command("list")
@click.option("--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True)
def inventory_list(state_dir: Path) -> None:
    records = _store(state_dir).list_environment_inventory()
    click.echo(json.dumps([record.model_dump(mode="json") for record in records], indent=2, sort_keys=True))


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


@promote.command("execute")
@click.option("--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--odoo-ai-root", type=click.Path(exists=True, file_okay=False, path_type=Path), required=True)
@click.option("--env-file", type=click.Path(exists=True, path_type=Path), default=None)
def promote_execute(
    state_dir: Path,
    input_file: Path,
    odoo_ai_root: Path,
    env_file: Path | None,
) -> None:
    request = CompatibilityPromotionRequest.model_validate(_load_json_file(input_file))
    record_store = _store(state_dir)
    resolved_artifact_id = _resolve_artifact_id_for_request(
        record_store=record_store,
        requested_artifact_id=request.artifact_id,
        source_git_ref=request.source_git_ref,
    )
    resolved_request = request.model_copy(update={"artifact_id": resolved_artifact_id})
    record_id = generate_promotion_record_id(
        context_name=resolved_request.context,
        from_instance_name=resolved_request.from_instance,
        to_instance_name=resolved_request.to_instance,
    )
    if resolved_request.dry_run:
        click.echo(
            json.dumps(
                build_compatibility_promotion_record(
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

    pending_record = build_compatibility_promotion_record(
        request=resolved_request,
        record_id=record_id,
        deployment_id="",
        deployment_status="pending",
    )
    record_path = record_store.write_promotion_record(pending_record)

    try:
        ship_request = _export_ship_request_via_odoo_ai(
            odoo_ai_root=odoo_ai_root,
            env_file=env_file,
            request=resolved_request,
        )
        _record_path, deployment_record = _execute_ship(
            state_dir=state_dir,
            odoo_ai_root=odoo_ai_root,
            env_file=env_file,
            request=ship_request,
        )
        if not isinstance(deployment_record, DeploymentRecord):
            raise click.ClickException("Ship execution returned an unexpected dry-run payload during promotion.")
        final_record = build_compatibility_promotion_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id=deployment_record.deploy.deployment_id,
            deployment_status=deployment_record.deploy.status,
        )
    except (subprocess.CalledProcessError, click.ClickException, json.JSONDecodeError):
        final_record = build_compatibility_promotion_record(
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
    click.echo(record_path)


@main.group()
def ship() -> None:
    """Ship workflow commands."""


@ship.command("plan")
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def ship_plan(input_file: Path) -> None:
    request = ShipRequest.model_validate(_load_json_file(input_file))
    click.echo(json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True))


@ship.command("execute")
@click.option("--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--odoo-ai-root", type=click.Path(exists=True, file_okay=False, path_type=Path), required=True)
@click.option("--env-file", type=click.Path(exists=True, path_type=Path), default=None)
def ship_execute(
    state_dir: Path,
    input_file: Path,
    odoo_ai_root: Path,
    env_file: Path | None,
) -> None:
    request = ShipRequest.model_validate(_load_json_file(input_file))
    record_path, _record = _execute_ship(
        state_dir=state_dir,
        odoo_ai_root=odoo_ai_root,
        env_file=env_file,
        request=request,
    )
    if record_path is not None:
        click.echo(record_path)
