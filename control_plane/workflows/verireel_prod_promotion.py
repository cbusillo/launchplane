from __future__ import annotations

import json
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    PromotionRecord,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.verireel_stable_deploy import (
    VeriReelStableDeployRequest,
    execute_verireel_stable_deploy,
)


DEFAULT_ROLLOUT_TIMEOUT_SECONDS = 300
DEFAULT_ROLLOUT_INTERVAL_SECONDS = 5
DEFAULT_ROLLOUT_PAGE_PATHS = ("/", "/sign-in")


class VeriReelRolloutVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    base_url: str = ""
    health_urls: tuple[str, ...] = ()
    started_at: str = ""
    finished_at: str = ""


class VeriReelProdPromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = "verireel"
    from_instance: str = "testing"
    to_instance: str = "prod"
    artifact_id: str
    source_git_ref: str
    backup_record_id: str
    promotion_record_id: str
    expected_build_revision: str = ""
    expected_build_tag: str = ""
    rollout_timeout_seconds: int = Field(default=DEFAULT_ROLLOUT_TIMEOUT_SECONDS, ge=1)
    rollout_interval_seconds: int = Field(default=DEFAULT_ROLLOUT_INTERVAL_SECONDS, ge=1)
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelProdPromotionRequest":
        if self.context != "verireel":
            raise ValueError("VeriReel prod promotion requires context 'verireel'.")
        if self.from_instance != "testing":
            raise ValueError("VeriReel prod promotion requires from_instance 'testing'.")
        if self.to_instance != "prod":
            raise ValueError("VeriReel prod promotion requires to_instance 'prod'.")
        if not self.artifact_id.strip():
            raise ValueError("VeriReel prod promotion requires artifact_id.")
        if not self.source_git_ref.strip():
            raise ValueError("VeriReel prod promotion requires source_git_ref.")
        if not self.backup_record_id.strip():
            raise ValueError("VeriReel prod promotion requires backup_record_id.")
        if not self.promotion_record_id.strip():
            raise ValueError("VeriReel prod promotion requires promotion_record_id.")
        return self


class VeriReelProdPromotionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promotion_record_id: str
    deployment_record_id: str = ""
    backup_record_id: str
    deploy_status: str
    rollout_status: str = "skipped"
    deploy_started_at: str = ""
    deploy_finished_at: str = ""
    target_name: str
    target_type: str
    target_id: str
    error_message: str = ""


def _default_target_name() -> str:
    return "ver-prod-app"


def _default_target_type() -> str:
    return "application"


def _default_deploy_mode() -> str:
    return "dokploy-application-api"


def _read_backup_gate_record(
    *,
    record_store: FilesystemRecordStore,
    record_id: str,
) -> BackupGateRecord:
    try:
        return record_store.read_backup_gate_record(record_id)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"VeriReel prod promotion requires stored backup gate record '{record_id}'."
        ) from exc


def _resolve_backup_gate_record(
    *,
    record_store: FilesystemRecordStore,
    request: VeriReelProdPromotionRequest,
) -> BackupGateRecord:
    backup_gate_record = _read_backup_gate_record(
        record_store=record_store,
        record_id=request.backup_record_id,
    )
    if backup_gate_record.context != request.context:
        raise click.ClickException(
            "Backup gate record context does not match VeriReel prod promotion request. "
            f"Record={backup_gate_record.context} request={request.context}."
        )
    if backup_gate_record.instance != request.to_instance:
        raise click.ClickException(
            "Backup gate record instance does not match VeriReel prod promotion destination. "
            f"Record={backup_gate_record.instance} request={request.to_instance}."
        )
    if not backup_gate_record.required:
        raise click.ClickException(
            f"Backup gate record '{backup_gate_record.record_id}' is marked required=false and cannot authorize VeriReel prod promotion."
        )
    if backup_gate_record.status != "pass":
        raise click.ClickException(
            f"Backup gate record '{backup_gate_record.record_id}' must have status=pass before VeriReel prod promotion."
        )
    return backup_gate_record


def _build_promotion_record(
    *,
    request: VeriReelProdPromotionRequest,
    backup_gate_record: BackupGateRecord,
    deployment_record: DeploymentRecord | None,
    deployment_record_id: str,
    target_id: str,
    target_name: str,
    target_type: str,
    deploy_status: str,
    deploy_started_at: str,
    deploy_finished_at: str,
    rollout_result: VeriReelRolloutVerificationResult | None,
) -> PromotionRecord:
    deploy_mode = _default_deploy_mode()
    if deployment_record is not None:
        deploy_mode = deployment_record.deploy.deploy_mode
    destination_health = HealthcheckEvidence(status="skipped")
    if rollout_result is not None:
        if rollout_result.status == "pass":
            destination_health = HealthcheckEvidence(
                verified=True,
                urls=rollout_result.health_urls,
                timeout_seconds=request.rollout_timeout_seconds,
                status="pass",
            )
        elif rollout_result.status == "fail":
            destination_health = HealthcheckEvidence(
                verified=True,
                urls=rollout_result.health_urls,
                timeout_seconds=request.rollout_timeout_seconds,
                status="fail",
            )
    return PromotionRecord(
        record_id=request.promotion_record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id=request.artifact_id),
        deployment_record_id=deployment_record_id,
        backup_record_id=backup_gate_record.record_id,
        context=request.context,
        from_instance=request.from_instance,
        to_instance=request.to_instance,
        source_health=HealthcheckEvidence(status="skipped"),
        backup_gate=BackupGateEvidence(
            required=backup_gate_record.required,
            status=backup_gate_record.status,
            evidence=dict(backup_gate_record.evidence),
        ),
        deploy=DeploymentEvidence(
            target_name=target_name,
            target_type=target_type,
            deploy_mode=deploy_mode,
            deployment_id=target_id,
            status="pass" if deploy_status == "pass" else "fail",
            started_at=deploy_started_at,
            finished_at=deploy_finished_at,
        ),
        post_deploy_update=PostDeployUpdateEvidence(),
        destination_health=destination_health,
    )


def _write_failed_promotion_record(
    *,
    record_store: FilesystemRecordStore,
    request: VeriReelProdPromotionRequest,
    backup_gate_record: BackupGateRecord | None,
    error_message: str,
    deployment_record_id: str = "",
    deploy_status: str = "fail",
    deploy_started_at: str = "",
    deploy_finished_at: str = "",
    target_name: str | None = None,
    target_type: str | None = None,
    target_id: str = "",
    rollout_result: VeriReelRolloutVerificationResult | None = None,
) -> None:
    resolved_target_name = target_name or _default_target_name()
    resolved_target_type = target_type or _default_target_type()
    destination_health = HealthcheckEvidence(status="skipped")
    if rollout_result is not None:
        destination_health = HealthcheckEvidence(
            verified=bool(rollout_result.health_urls),
            urls=rollout_result.health_urls,
            timeout_seconds=(request.rollout_timeout_seconds if rollout_result.health_urls else None),
            status="fail" if rollout_result.status == "fail" else rollout_result.status,
        )
    record_store.write_promotion_record(
        PromotionRecord(
            record_id=request.promotion_record_id,
            artifact_identity=ArtifactIdentityReference(artifact_id=request.artifact_id),
            deployment_record_id=deployment_record_id,
            backup_record_id=request.backup_record_id,
            context=request.context,
            from_instance=request.from_instance,
            to_instance=request.to_instance,
            source_health=HealthcheckEvidence(status="skipped"),
            backup_gate=BackupGateEvidence(
                required=(backup_gate_record.required if backup_gate_record is not None else True),
                status=(backup_gate_record.status if backup_gate_record is not None else "fail"),
                evidence=(
                    dict(backup_gate_record.evidence)
                    if backup_gate_record is not None
                    else {"backup_record_id": request.backup_record_id, "error": error_message}
                ),
            ),
            deploy=DeploymentEvidence(
                target_name=resolved_target_name,
                target_type=resolved_target_type,
                deploy_mode=_default_deploy_mode(),
                deployment_id=target_id,
                status=deploy_status,
                started_at=deploy_started_at,
                finished_at=deploy_finished_at,
            ),
            post_deploy_update=PostDeployUpdateEvidence(
                attempted=False,
                status="skipped",
                detail=error_message,
            ),
            destination_health=destination_health,
        )
    )


def _resolve_rollout_base_urls(
    *,
    control_plane_root: Path,
    request: VeriReelProdPromotionRequest,
) -> tuple[str, ...]:
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=request.context,
        instance_name=request.to_instance,
    )
    if target_definition is None:
        raise click.ClickException(
            f"No Dokploy target definition found for {request.context}/{request.to_instance}."
        )
    environment_values = control_plane_dokploy.read_control_plane_environment_values(
        control_plane_root=control_plane_root,
    )
    base_urls = control_plane_dokploy.resolve_healthcheck_base_urls(
        target_definition=target_definition,
        environment_values=environment_values,
    )
    if not base_urls:
        raise click.ClickException(
            f"No rollout base URL configured for {request.context}/{request.to_instance}."
        )
    return base_urls


def _fetch_url_text(url: str, *, accept: str) -> tuple[int, str]:
    request = Request(
        url,
        headers={
            "Accept": accept,
            "Cache-Control": "no-store",
        },
    )
    with urlopen(request, timeout=15) as response:
        return response.status, response.read().decode("utf-8")


def _validate_health_payload(
    payload: object,
    *,
    health_url: str,
    expected_build_revision: str,
    expected_build_tag: str,
) -> str | None:
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return f"health payload from {health_url} did not report ok=true"
    if expected_build_revision and payload.get("buildRevision") != expected_build_revision:
        return (
            f"health payload from {health_url} reported buildRevision "
            f"'{payload.get('buildRevision', 'unknown')}' instead of expected "
            f"'{expected_build_revision}'"
        )
    if expected_build_tag and payload.get("buildTag") != expected_build_tag:
        return (
            f"health payload from {health_url} reported buildTag "
            f"'{payload.get('buildTag', 'unknown')}' instead of expected "
            f"'{expected_build_tag}'"
        )
    return None


def _assert_rollout_pages(base_url: str) -> None:
    for page_path in DEFAULT_ROLLOUT_PAGE_PATHS:
        page_url = f"{base_url.rstrip('/')}{page_path}"
        try:
            status_code, response_text = _fetch_url_text(
                page_url,
                accept="text/html,application/json",
            )
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            raise click.ClickException(
                f"VeriReel prod rollout page verification failed for {page_url}: {exc}"
            ) from exc
        if status_code < 200 or status_code >= 300:
            raise click.ClickException(
                f"VeriReel prod rollout page verification expected {page_url} to return 2xx, received {status_code}."
            )
        if "VeriReel" not in response_text:
            raise click.ClickException(
                f'VeriReel prod rollout page verification expected {page_url} to include "VeriReel".'
            )


def _verify_rollout(
    *,
    control_plane_root: Path,
    request: VeriReelProdPromotionRequest,
) -> VeriReelRolloutVerificationResult:
    started_at = utc_now_timestamp()
    base_urls = _resolve_rollout_base_urls(
        control_plane_root=control_plane_root,
        request=request,
    )
    health_urls = tuple(f"{base_url.rstrip('/')}/api/health" for base_url in base_urls)
    last_error = "health endpoint not checked yet"
    deadline = time.monotonic() + request.rollout_timeout_seconds
    while time.monotonic() <= deadline:
        for base_url, health_url in zip(base_urls, health_urls, strict=False):
            try:
                status_code, response_text = _fetch_url_text(
                    health_url,
                    accept="application/json,text/html",
                )
                if status_code < 200 or status_code >= 300:
                    last_error = f"received {status_code} from {health_url}"
                    continue
                payload = json.loads(response_text)
                validation_error = _validate_health_payload(
                    payload,
                    health_url=health_url,
                    expected_build_revision=request.expected_build_revision,
                    expected_build_tag=request.expected_build_tag,
                )
                if validation_error is not None:
                    last_error = validation_error
                    continue
                _assert_rollout_pages(base_url)
                return VeriReelRolloutVerificationResult(
                    status="pass",
                    base_url=base_url,
                    health_urls=(health_url,),
                    started_at=started_at,
                    finished_at=utc_now_timestamp(),
                )
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                last_error = str(exc)
            except click.ClickException as exc:
                raise click.ClickException(str(exc)) from exc
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break
        time.sleep(min(request.rollout_interval_seconds, remaining_seconds))
    raise click.ClickException(f"VeriReel prod rollout verification timed out: {last_error}")


def execute_verireel_prod_promotion(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    request: VeriReelProdPromotionRequest,
) -> VeriReelProdPromotionResult:
    try:
        backup_gate_record = _resolve_backup_gate_record(
            record_store=record_store,
            request=request,
        )
    except click.ClickException as exc:
        error_message = str(exc)
        _write_failed_promotion_record(
            record_store=record_store,
            request=request,
            backup_gate_record=None,
            error_message=error_message,
        )
        return VeriReelProdPromotionResult(
            promotion_record_id=request.promotion_record_id,
            backup_record_id=request.backup_record_id,
            deploy_status="fail",
            rollout_status="skipped",
            target_name=_default_target_name(),
            target_type=_default_target_type(),
            target_id="",
            error_message=error_message,
        )

    deployment_result = execute_verireel_stable_deploy(
        control_plane_root=control_plane_root,
        record_store=record_store,
        request=VeriReelStableDeployRequest(
            context=request.context,
            instance=request.to_instance,
            artifact_id=request.artifact_id,
            source_git_ref=request.source_git_ref,
            no_cache=request.no_cache,
        ),
    )

    deployment_record = None
    if deployment_result.deployment_record_id:
        try:
            deployment_record = record_store.read_deployment_record(
                deployment_result.deployment_record_id
            )
        except FileNotFoundError:
            deployment_record = None

    if deployment_result.deploy_status != "pass":
        promotion_record = _build_promotion_record(
            request=request,
            backup_gate_record=backup_gate_record,
            deployment_record=deployment_record,
            deployment_record_id=deployment_result.deployment_record_id,
            target_id=deployment_result.target_id,
            target_name=deployment_result.target_name,
            target_type=deployment_result.target_type,
            deploy_status=deployment_result.deploy_status,
            deploy_started_at=deployment_result.deploy_started_at,
            deploy_finished_at=deployment_result.deploy_finished_at,
            rollout_result=None,
        )
        record_store.write_promotion_record(promotion_record)
        return VeriReelProdPromotionResult(
            promotion_record_id=request.promotion_record_id,
            deployment_record_id=deployment_result.deployment_record_id,
            backup_record_id=backup_gate_record.record_id,
            deploy_status=deployment_result.deploy_status,
            rollout_status="skipped",
            deploy_started_at=deployment_result.deploy_started_at,
            deploy_finished_at=deployment_result.deploy_finished_at,
            target_name=deployment_result.target_name,
            target_type=deployment_result.target_type,
            target_id=deployment_result.target_id,
            error_message=deployment_result.error_message,
        )

    try:
        rollout_result = _verify_rollout(
            control_plane_root=control_plane_root,
            request=request,
        )
    except click.ClickException as exc:
        error_message = str(exc)
        failed_rollout_result = VeriReelRolloutVerificationResult(status="fail")
        try:
            base_urls = _resolve_rollout_base_urls(
                control_plane_root=control_plane_root,
                request=request,
            )
            failed_rollout_result = VeriReelRolloutVerificationResult(
                status="fail",
                base_url=base_urls[0],
                health_urls=(f"{base_urls[0].rstrip('/')}/api/health",),
            )
        except click.ClickException:
            pass
        _write_failed_promotion_record(
            record_store=record_store,
            request=request,
            backup_gate_record=backup_gate_record,
            error_message=error_message,
            deployment_record_id=deployment_result.deployment_record_id,
            deploy_status="pass",
            deploy_started_at=deployment_result.deploy_started_at,
            deploy_finished_at=deployment_result.deploy_finished_at,
            target_name=deployment_result.target_name,
            target_type=deployment_result.target_type,
            target_id=deployment_result.target_id,
            rollout_result=failed_rollout_result,
        )
        return VeriReelProdPromotionResult(
            promotion_record_id=request.promotion_record_id,
            deployment_record_id=deployment_result.deployment_record_id,
            backup_record_id=backup_gate_record.record_id,
            deploy_status=deployment_result.deploy_status,
            rollout_status="fail",
            deploy_started_at=deployment_result.deploy_started_at,
            deploy_finished_at=deployment_result.deploy_finished_at,
            target_name=deployment_result.target_name,
            target_type=deployment_result.target_type,
            target_id=deployment_result.target_id,
            error_message=error_message,
        )

    promotion_record = _build_promotion_record(
        request=request,
        backup_gate_record=backup_gate_record,
        deployment_record=deployment_record,
        deployment_record_id=deployment_result.deployment_record_id,
        target_id=deployment_result.target_id,
        target_name=deployment_result.target_name,
        target_type=deployment_result.target_type,
        deploy_status=deployment_result.deploy_status,
        deploy_started_at=deployment_result.deploy_started_at,
        deploy_finished_at=deployment_result.deploy_finished_at,
        rollout_result=rollout_result,
    )
    record_store.write_promotion_record(promotion_record)
    return VeriReelProdPromotionResult(
        promotion_record_id=request.promotion_record_id,
        deployment_record_id=deployment_result.deployment_record_id,
        backup_record_id=backup_gate_record.record_id,
        deploy_status=deployment_result.deploy_status,
        rollout_status=rollout_result.status,
        deploy_started_at=deployment_result.deploy_started_at,
        deploy_finished_at=deployment_result.deploy_finished_at,
        target_name=deployment_result.target_name,
        target_type=deployment_result.target_type,
        target_id=deployment_result.target_id,
        error_message=deployment_result.error_message,
    )
