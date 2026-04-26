from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import (
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    PromotionRecord,
    RollbackExecutionEvidence,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.ship_request import ShipRequest
from control_plane.release_tuples import build_release_tuple_record_from_artifact_manifest
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.odoo_post_deploy import OdooPostDeployRequest, execute_odoo_post_deploy
from control_plane.workflows.ship import (
    build_deployment_record,
    generate_deployment_record_id,
    utc_now_timestamp,
)

ARTIFACT_IMAGE_REFERENCE_ENV_KEY = "DOCKER_IMAGE_REFERENCE"
SUPPORTED_ODOO_CONTEXTS = {"cm", "opw"}


@dataclass(frozen=True)
class _RollbackSource:
    artifact_id: str
    source_git_ref: str
    result_source_channel: str
    promoted_from_instance: str
    snapshot_name: str
    detail: str


class OdooProdRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str = "prod"
    source_channel: Literal["testing"] = "testing"
    promotion_record_id: str = ""
    artifact_id: str = ""
    reason: str = ""
    wait: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)
    verify_health: bool = True
    health_timeout_seconds: int | None = Field(default=None, ge=1)
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooProdRollbackRequest":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.promotion_record_id = self.promotion_record_id.strip()
        self.artifact_id = self.artifact_id.strip()
        self.reason = self.reason.strip()
        if self.context not in SUPPORTED_ODOO_CONTEXTS:
            supported = ", ".join(sorted(SUPPORTED_ODOO_CONTEXTS))
            raise ValueError(
                f"Odoo prod rollback supports contexts {supported}; got {self.context!r}."
            )
        if self.instance != "prod":
            raise ValueError("Odoo prod rollback requires instance 'prod'.")
        if self.verify_health and not self.wait:
            raise ValueError("Odoo prod rollback health verification requires wait=true.")
        return self


class OdooProdRollbackResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    source_channel: str
    artifact_id: str
    promotion_record_id: str
    deployment_record_id: str = ""
    release_tuple_id: str = ""
    rollback_status: Literal["pass", "fail"]
    rollback_health_status: Literal["pass", "fail", "skipped"] = "skipped"
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    error_message: str = ""


def _artifact_image_reference_from_manifest(manifest: ArtifactIdentityManifest) -> str:
    return f"{manifest.image.repository}@{manifest.image.digest}"


def _read_source_release_tuple(
    *,
    record_store: FilesystemRecordStore,
    request: OdooProdRollbackRequest,
) -> ReleaseTupleRecord:
    try:
        return record_store.read_release_tuple_record(
            context_name=request.context,
            channel_name=request.source_channel,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Odoo prod rollback requires a DB-backed {request.context}/{request.source_channel} release tuple."
        ) from exc


def _read_artifact_manifest(
    *,
    record_store: FilesystemRecordStore,
    artifact_id: str,
) -> ArtifactIdentityManifest:
    try:
        return record_store.read_artifact_manifest(artifact_id)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Odoo prod rollback requires artifact manifest {artifact_id!r} in Launchplane records."
        ) from exc


def _resolve_rollback_source(
    *,
    request: OdooProdRollbackRequest,
    artifact_manifest: ArtifactIdentityManifest,
    source_tuple: ReleaseTupleRecord | None,
) -> _RollbackSource:
    if source_tuple is not None:
        source_git_ref = source_tuple.repo_shas.get(
            f"tenant-{request.context}", artifact_manifest.source_commit
        )
        return _RollbackSource(
            artifact_id=source_tuple.artifact_id,
            source_git_ref=source_git_ref,
            result_source_channel=request.source_channel,
            promoted_from_instance=request.source_channel,
            snapshot_name=f"release-tuple:{source_tuple.tuple_id}",
            detail=(
                f"Rolled {request.context}/{request.instance} back to "
                f"{request.source_channel} release tuple {source_tuple.tuple_id}."
            ),
        )
    return _RollbackSource(
        artifact_id=artifact_manifest.artifact_id,
        source_git_ref=artifact_manifest.source_commit,
        result_source_channel="artifact",
        promoted_from_instance="explicit-artifact",
        snapshot_name=f"artifact:{artifact_manifest.artifact_id}",
        detail=(
            f"Rolled {request.context}/{request.instance} back to explicit "
            f"Launchplane artifact {artifact_manifest.artifact_id}."
        ),
    )


def _read_prod_inventory(
    *,
    record_store: FilesystemRecordStore,
    request: OdooProdRollbackRequest,
) -> EnvironmentInventory:
    try:
        return record_store.read_environment_inventory(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Odoo prod rollback requires current inventory for {request.context}/{request.instance}."
        ) from exc


def _read_promotion_record(
    *,
    record_store: FilesystemRecordStore,
    promotion_record_id: str,
) -> PromotionRecord:
    try:
        return record_store.read_promotion_record(promotion_record_id)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Odoo prod rollback requires promotion record {promotion_record_id!r}."
        ) from exc


def _resolve_promotion_record(
    *,
    record_store: FilesystemRecordStore,
    request: OdooProdRollbackRequest,
) -> PromotionRecord:
    promotion_record_id = request.promotion_record_id
    if not promotion_record_id:
        promotion_record_id = _read_prod_inventory(
            record_store=record_store,
            request=request,
        ).promotion_record_id.strip()
    if not promotion_record_id:
        raise click.ClickException(
            f"Odoo prod rollback could not resolve a current promotion record for {request.context}/{request.instance}."
        )
    promotion_record = _read_promotion_record(
        record_store=record_store,
        promotion_record_id=promotion_record_id,
    )
    if (
        promotion_record.context != request.context
        or promotion_record.to_instance != request.instance
    ):
        raise click.ClickException(
            "Odoo prod rollback promotion record does not match the requested prod lane. "
            f"Record={promotion_record.context}/{promotion_record.to_instance} "
            f"request={request.context}/{request.instance}."
        )
    return promotion_record


def _read_target_definition(
    *,
    record_store: FilesystemRecordStore,
    request: OdooProdRollbackRequest,
) -> control_plane_dokploy.DokployTargetDefinition:
    try:
        target_record = record_store.read_dokploy_target_record(
            context_name=request.context,
            instance_name=request.instance,
        )
        target_id_record = record_store.read_dokploy_target_id_record(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Odoo prod rollback requires DB-backed Dokploy target records for {request.context}/{request.instance}."
        ) from exc
    return _build_dokploy_target_definition(
        target_record=target_record,
        target_id=target_id_record.target_id,
    )


def _build_dokploy_target_definition(
    *,
    target_record: DokployTargetRecord,
    target_id: str,
) -> control_plane_dokploy.DokployTargetDefinition:
    payload = target_record.model_dump(
        mode="json",
        exclude={"schema_version", "updated_at", "source_label"},
    )
    payload["target_id"] = target_id
    return control_plane_dokploy.DokployTargetDefinition.model_validate(payload)


def _resolve_ship_request(
    *,
    request: OdooProdRollbackRequest,
    rollback_source: _RollbackSource,
    artifact_manifest: ArtifactIdentityManifest,
    target_definition: control_plane_dokploy.DokployTargetDefinition,
) -> ShipRequest:
    health_timeout_seconds = control_plane_dokploy.resolve_ship_health_timeout_seconds(
        health_timeout_override_seconds=request.health_timeout_seconds,
        target_definition=target_definition,
    )
    health_urls = control_plane_dokploy.resolve_ship_healthcheck_urls(
        target_definition=target_definition,
        environment_values=target_definition.env,
    )
    should_verify_health = request.verify_health and request.wait
    if should_verify_health and not health_urls:
        raise click.ClickException(
            "Odoo prod rollback health verification requested but no target health URL was resolved."
        )
    configured_ship_mode = control_plane_dokploy.resolve_dokploy_ship_mode(
        request.context,
        request.instance,
        target_definition.env,
    )
    deploy_mode = (
        f"dokploy-{target_definition.target_type}-api"
        if configured_ship_mode == "auto"
        else f"dokploy-{configured_ship_mode}-api"
    )
    return ShipRequest(
        artifact_id=rollback_source.artifact_id,
        context=request.context,
        instance=request.instance,
        source_git_ref=rollback_source.source_git_ref,
        target_name=target_definition.target_name.strip()
        or f"{request.context}-{request.instance}",
        target_type=target_definition.target_type,
        deploy_mode=deploy_mode,
        wait=request.wait,
        timeout_seconds=request.timeout_seconds,
        verify_health=should_verify_health,
        health_timeout_seconds=health_timeout_seconds,
        dry_run=False,
        no_cache=request.no_cache,
        allow_dirty=False,
        destination_health=HealthcheckEvidence(
            urls=health_urls,
            timeout_seconds=health_timeout_seconds,
            status="pending" if should_verify_health else "skipped",
        ),
    )


def _sync_artifact_image_reference_for_target(
    *,
    control_plane_root: Path,
    target_definition: control_plane_dokploy.DokployTargetDefinition,
    artifact_manifest: ArtifactIdentityManifest,
) -> None:
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_definition.target_type,
        target_id=target_definition.target_id,
    )
    env_map = control_plane_dokploy.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    desired_image_reference = _artifact_image_reference_from_manifest(artifact_manifest)
    if env_map.get(ARTIFACT_IMAGE_REFERENCE_ENV_KEY, "") == desired_image_reference:
        return
    env_map[ARTIFACT_IMAGE_REFERENCE_ENV_KEY] = desired_image_reference
    control_plane_dokploy.update_dokploy_target_env(
        host=host,
        token=token,
        target_type=target_definition.target_type,
        target_id=target_definition.target_id,
        target_payload=target_payload,
        env_text=control_plane_dokploy.serialize_dokploy_env_text(env_map),
    )


def _trigger_dokploy_deploy(
    *,
    control_plane_root: Path,
    request: ShipRequest,
    target_definition: control_plane_dokploy.DokployTargetDefinition,
) -> None:
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    latest_before = None
    if request.wait:
        latest_before = control_plane_dokploy.latest_deployment_for_target(
            host=host,
            token=token,
            target_type=target_definition.target_type,
            target_id=target_definition.target_id,
        )
    control_plane_dokploy.trigger_deployment(
        host=host,
        token=token,
        target_type=target_definition.target_type,
        target_id=target_definition.target_id,
        no_cache=request.no_cache,
    )
    if not request.wait:
        return
    deploy_timeout_seconds = control_plane_dokploy.resolve_ship_timeout_seconds(
        timeout_override_seconds=request.timeout_seconds,
        target_definition=target_definition,
    )
    control_plane_dokploy.wait_for_target_deployment(
        host=host,
        token=token,
        target_type=target_definition.target_type,
        target_id=target_definition.target_id,
        before_key=control_plane_dokploy.deployment_key(latest_before),
        timeout_seconds=deploy_timeout_seconds,
    )


def _wait_for_healthcheck(*, url: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = ""
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


def _verify_healthchecks(*, request: ShipRequest) -> None:
    if not request.wait or not request.verify_health:
        return
    if not request.destination_health.urls or request.destination_health.timeout_seconds is None:
        raise click.ClickException(
            "Odoo prod rollback health verification is missing URLs or timeout."
        )
    health_errors: list[str] = []
    for health_url in request.destination_health.urls:
        try:
            _wait_for_healthcheck(
                url=health_url,
                timeout_seconds=request.destination_health.timeout_seconds,
            )
            return
        except click.ClickException as error:
            health_errors.append(str(error))
    raise click.ClickException(
        "Odoo prod rollback health verification failed for all resolved URLs:\n"
        + "\n".join(health_errors)
    )


def _rollback_health_evidence(*, request: ShipRequest, status: str) -> HealthcheckEvidence:
    return request.destination_health.model_copy(
        update={
            "verified": status in {"pass", "fail"} and bool(request.destination_health.urls),
            "status": status,
        }
    )


def _write_rollback_state(
    *,
    record_store: FilesystemRecordStore,
    promotion_record: PromotionRecord,
    snapshot_name: str,
    request: ShipRequest,
    status: str,
    health_status: str,
    started_at: str,
    finished_at: str,
    detail: str,
) -> PromotionRecord:
    updated_record = promotion_record.model_copy(
        update={
            "rollback": RollbackExecutionEvidence(
                attempted=True,
                status=status,
                detail=detail,
                snapshot_name=snapshot_name,
                started_at=started_at,
                finished_at=finished_at,
            ),
            "rollback_health": _rollback_health_evidence(request=request, status=health_status),
        }
    )
    record_store.write_promotion_record(updated_record)
    return updated_record


def execute_odoo_prod_rollback(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    request: OdooProdRollbackRequest,
) -> OdooProdRollbackResult:
    source_tuple: ReleaseTupleRecord | None = None
    if request.artifact_id:
        artifact_manifest = _read_artifact_manifest(
            record_store=record_store,
            artifact_id=request.artifact_id,
        )
    else:
        source_tuple = _read_source_release_tuple(record_store=record_store, request=request)
        artifact_manifest = _read_artifact_manifest(
            record_store=record_store,
            artifact_id=source_tuple.artifact_id,
        )
    rollback_source = _resolve_rollback_source(
        request=request,
        artifact_manifest=artifact_manifest,
        source_tuple=source_tuple,
    )
    promotion_record = _resolve_promotion_record(record_store=record_store, request=request)
    target_definition = _read_target_definition(record_store=record_store, request=request)
    ship_request = _resolve_ship_request(
        request=request,
        rollback_source=rollback_source,
        artifact_manifest=artifact_manifest,
        target_definition=target_definition,
    )
    resolved_target = ResolvedTargetEvidence(
        target_type=target_definition.target_type,
        target_id=target_definition.target_id,
        target_name=target_definition.target_name.strip() or ship_request.target_name,
    )
    deployment_record_id = generate_deployment_record_id(
        context_name=request.context,
        instance_name=request.instance,
    )
    started_at = utc_now_timestamp()
    record_store.write_deployment_record(
        build_deployment_record(
            request=ship_request,
            record_id=deployment_record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="pending",
            started_at=started_at,
            finished_at="",
            resolved_target=resolved_target,
            post_deploy_update=PostDeployUpdateEvidence(attempted=True, status="pending"),
        )
    )
    _write_rollback_state(
        record_store=record_store,
        promotion_record=promotion_record,
        snapshot_name=rollback_source.snapshot_name,
        request=ship_request,
        status="pending",
        health_status="skipped",
        started_at=started_at,
        finished_at="",
        detail="Odoo prod rollback deployment is pending.",
    )

    deploy_completed = False
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    health_status: Literal["pass", "fail", "skipped"] = "skipped"
    try:
        _sync_artifact_image_reference_for_target(
            control_plane_root=control_plane_root,
            target_definition=target_definition,
            artifact_manifest=artifact_manifest,
        )
        _trigger_dokploy_deploy(
            control_plane_root=control_plane_root,
            request=ship_request,
            target_definition=target_definition,
        )
        deploy_completed = True
        post_deploy_result = execute_odoo_post_deploy(
            control_plane_root=control_plane_root,
            record_store=record_store,
            request=OdooPostDeployRequest(context=request.context, instance=request.instance),
        )
        post_deploy_status = post_deploy_result.post_deploy_status
        if post_deploy_result.post_deploy_status != "pass":
            raise click.ClickException(
                post_deploy_result.error_message or "Odoo post-deploy failed."
            )
        _verify_healthchecks(request=ship_request)
        health_status = "pass" if ship_request.verify_health else "skipped"
    except (click.ClickException, OSError) as error:
        finished_at = utc_now_timestamp()
        if deploy_completed and health_status == "skipped" and ship_request.verify_health:
            health_status = "fail"
        final_deployment = build_deployment_record(
            request=ship_request,
            record_id=deployment_record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="pass" if deploy_completed else "fail",
            started_at=started_at,
            finished_at=finished_at,
            resolved_target=resolved_target,
            post_deploy_update=PostDeployUpdateEvidence(
                attempted=post_deploy_status in {"pass", "fail"},
                status=post_deploy_status if deploy_completed else "skipped",
                detail=str(error),
            ),
            destination_health=_rollback_health_evidence(
                request=ship_request, status=health_status
            ),
        )
        record_store.write_deployment_record(final_deployment)
        _write_rollback_state(
            record_store=record_store,
            promotion_record=promotion_record,
            snapshot_name=rollback_source.snapshot_name,
            request=ship_request,
            status="fail",
            health_status=health_status,
            started_at=started_at,
            finished_at=finished_at,
            detail=str(error),
        )
        return OdooProdRollbackResult(
            context=request.context,
            instance=request.instance,
            source_channel=rollback_source.result_source_channel,
            artifact_id=rollback_source.artifact_id,
            promotion_record_id=promotion_record.record_id,
            deployment_record_id=deployment_record_id,
            rollback_status="fail",
            rollback_health_status=health_status,
            post_deploy_status=post_deploy_status,
            error_message=str(error),
        )

    finished_at = utc_now_timestamp()
    final_deployment = build_deployment_record(
        request=ship_request,
        record_id=deployment_record_id,
        deployment_id="control-plane-dokploy",
        deployment_status="pass",
        started_at=started_at,
        finished_at=finished_at,
        resolved_target=resolved_target,
        post_deploy_update=PostDeployUpdateEvidence(
            attempted=True,
            status=post_deploy_status,
            detail="Odoo post-deploy completed after prod rollback.",
        ),
        destination_health=_rollback_health_evidence(request=ship_request, status=health_status),
    )
    record_store.write_deployment_record(final_deployment)
    inventory_record = build_environment_inventory(
        deployment_record=final_deployment,
        updated_at=finished_at,
        promotion_record_id=promotion_record.record_id,
        promoted_from_instance=rollback_source.promoted_from_instance,
    )
    record_store.write_environment_inventory(inventory_record)
    release_tuple = build_release_tuple_record_from_artifact_manifest(
        context_name=request.context,
        channel_name=request.instance,
        artifact_manifest=artifact_manifest,
        deployment_record_id=deployment_record_id,
        minted_at=finished_at,
    )
    record_store.write_release_tuple_record(release_tuple)
    _write_rollback_state(
        record_store=record_store,
        promotion_record=promotion_record,
        snapshot_name=rollback_source.snapshot_name,
        request=ship_request,
        status="pass",
        health_status=health_status,
        started_at=started_at,
        finished_at=finished_at,
        detail=rollback_source.detail,
    )
    return OdooProdRollbackResult(
        context=request.context,
        instance=request.instance,
        source_channel=rollback_source.result_source_channel,
        artifact_id=rollback_source.artifact_id,
        promotion_record_id=promotion_record.record_id,
        deployment_record_id=deployment_record_id,
        release_tuple_id=release_tuple.tuple_id,
        rollback_status="pass",
        rollback_health_status=health_status,
        post_deploy_status=post_deploy_status,
    )
