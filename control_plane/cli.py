import json
import os
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.promotion_record import CompatibilityPromotionRequest, PromotionRecord
from control_plane.contracts.ship_request import CompatibilityShipRequest
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.promote import (
    build_compatibility_promotion_record,
    build_promotion_record,
    generate_promotion_record_id,
)
from control_plane.workflows.ship import (
    build_compatibility_deployment_record,
    generate_deployment_record_id,
    utc_now_timestamp,
)


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


def _apply_branch_sync(*, odoo_ai_root: Path, request: CompatibilityShipRequest) -> CompatibilityShipRequest:
    branch_sync = request.branch_sync
    if branch_sync is None or not branch_sync.branch_update_required or branch_sync.applied:
        return request

    _run_command(
        [
            "git",
            "push",
            "origin",
            f"+{branch_sync.source_commit}:refs/heads/{branch_sync.target_branch}",
        ],
        cwd=odoo_ai_root,
    )
    return request.model_copy(update={"branch_sync": branch_sync.model_copy(update={"applied": True})})


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


def _verify_ship_healthchecks(*, request: CompatibilityShipRequest) -> None:
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


def _resolve_dokploy_target_via_odoo_ai(
    *,
    odoo_ai_root: Path,
    env_file: Path | None,
    request: CompatibilityShipRequest,
) -> ResolvedTargetEvidence:
    command = [
        "uv",
        "run",
        "--project",
        str(odoo_ai_root),
        "platform",
        "compatibility-resolve-ship-target",
        "--context",
        request.context,
        "--instance",
        request.instance,
        "--target-type",
        request.target_type,
    ]
    if env_file is not None:
        command.extend(["--env-file", str(env_file)])
    payload = json.loads(_run_command_capture(command, cwd=odoo_ai_root))
    return ResolvedTargetEvidence.model_validate(payload)


def _resolve_artifact_id_for_request(
    *,
    record_store: FilesystemRecordStore,
    requested_artifact_id: str,
    source_git_ref: str,
) -> str:
    matching_manifests = record_store.find_artifact_manifests_by_commit(source_git_ref)
    if len(matching_manifests) == 1:
        return matching_manifests[0].artifact_id
    return requested_artifact_id


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

    ship_command = [
        "uv",
        "run",
        "--project",
        str(odoo_ai_root),
        "platform",
        "ship",
        "--context",
        resolved_request.context,
        "--instance",
        resolved_request.to_instance,
        "--source-ref",
        resolved_request.source_git_ref,
        "--skip-gate",
    ]
    if env_file is not None:
        ship_command.extend(["--env-file", str(env_file)])
    if resolved_request.wait:
        ship_command.append("--wait")
    else:
        ship_command.append("--no-wait")
    if resolved_request.timeout_seconds is not None:
        ship_command.extend(["--timeout", str(resolved_request.timeout_seconds)])
    if resolved_request.verify_health:
        ship_command.append("--verify-health")
    else:
        ship_command.append("--no-verify-health")
    if resolved_request.health_timeout_seconds is not None:
        ship_command.extend(["--health-timeout", str(resolved_request.health_timeout_seconds)])
    if resolved_request.no_cache:
        ship_command.append("--no-cache")
    if resolved_request.allow_dirty:
        ship_command.append("--allow-dirty")

    try:
        _run_command(ship_command)
        final_record = build_compatibility_promotion_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="delegated-odoo-ai-ship",
            deployment_status="pass",
        )
    except subprocess.CalledProcessError:
        final_record = build_compatibility_promotion_record(
            request=resolved_request,
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


@ship.command("compatibility-execute")
@click.option("--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--odoo-ai-root", type=click.Path(exists=True, file_okay=False, path_type=Path), required=True)
@click.option("--env-file", type=click.Path(exists=True, path_type=Path), default=None)
def ship_compatibility_execute(
    state_dir: Path,
    input_file: Path,
    odoo_ai_root: Path,
    env_file: Path | None,
) -> None:
    request = CompatibilityShipRequest.model_validate(_load_json_file(input_file))
    if request.dry_run:
        click.echo(json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True))
        return

    ship_command = [
        "uv",
        "run",
        "--project",
        str(odoo_ai_root),
        "platform",
        "compatibility-ship-worker",
        "--context",
        request.context,
        "--instance",
        request.instance,
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
    ship_command.append("--no-verify-health")
    if request.health_timeout_seconds is not None:
        ship_command.extend(["--health-timeout", str(request.health_timeout_seconds)])
    if request.no_cache:
        ship_command.append("--no-cache")
    if request.allow_dirty:
        ship_command.append("--allow-dirty")
    record_store = _store(state_dir)
    record_id = generate_deployment_record_id(
        context_name=request.context,
        instance_name=request.instance,
    )
    started_at = utc_now_timestamp()
    pending_record = build_compatibility_deployment_record(
        request=request,
        record_id=record_id,
        deployment_id="delegated-odoo-ai-ship",
        deployment_status="pending",
        started_at=started_at,
        finished_at="",
    )
    record_path = record_store.write_deployment_record(pending_record)

    try:
        delegated_request = _apply_branch_sync(odoo_ai_root=odoo_ai_root, request=request)
        resolved_target = _resolve_dokploy_target_via_odoo_ai(
            odoo_ai_root=odoo_ai_root,
            env_file=env_file,
            request=delegated_request,
        )
    except (subprocess.CalledProcessError, click.ClickException, json.JSONDecodeError):
        final_record = build_compatibility_deployment_record(
            request=request,
            record_id=record_id,
            deployment_id="delegated-odoo-ai-ship",
            deployment_status="fail",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
        )
        record_store.write_deployment_record(final_record)
        raise

    if delegated_request.branch_sync is not None:
        ship_command.extend(
            [
                "--branch-sync-source-ref",
                delegated_request.branch_sync.source_git_ref,
                "--branch-sync-source-commit",
                delegated_request.branch_sync.source_commit,
                "--branch-sync-target-branch",
                delegated_request.branch_sync.target_branch,
                "--branch-sync-remote-branch-commit-before",
                delegated_request.branch_sync.remote_branch_commit_before,
            ]
        )
        if delegated_request.branch_sync.branch_update_required:
            ship_command.append("--branch-sync-update-required")
        else:
            ship_command.append("--no-branch-sync-update-required")
        if delegated_request.branch_sync.applied:
            ship_command.append("--branch-sync-applied")
        else:
            ship_command.append("--no-branch-sync-applied")
    ship_command.extend(
        [
            "--resolved-target-type",
            resolved_target.target_type,
            "--resolved-target-id",
            resolved_target.target_id,
            "--resolved-target-name",
            resolved_target.target_name,
        ]
    )

    try:
        _run_command(ship_command)
        _verify_ship_healthchecks(request=delegated_request)
        final_record = build_compatibility_deployment_record(
            request=delegated_request,
            record_id=record_id,
            deployment_id="delegated-odoo-ai-ship",
            deployment_status="pass",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            resolved_target=resolved_target,
        )
    except subprocess.CalledProcessError:
        final_record = build_compatibility_deployment_record(
            request=delegated_request,
            record_id=record_id,
            deployment_id="delegated-odoo-ai-ship",
            deployment_status="fail",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            resolved_target=resolved_target,
        )
        record_store.write_deployment_record(final_record)
        raise

    record_store.write_deployment_record(final_record)
    click.echo(record_path)
