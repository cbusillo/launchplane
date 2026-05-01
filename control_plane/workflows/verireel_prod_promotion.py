from __future__ import annotations

from pathlib import Path
import time

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
from control_plane.workflows.verireel_stable_deploy import (
    VeriReelStableDeployRequest,
    execute_verireel_stable_deploy,
)
from control_plane.workflows.verireel_rollout import (
    DEFAULT_ROLLOUT_INTERVAL_SECONDS,
    DEFAULT_ROLLOUT_TIMEOUT_SECONDS,
    VeriReelRolloutVerificationResult,
    resolve_verireel_rollout_base_urls,
    verify_verireel_rollout,
)


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
    migration_status: str = "skipped"
    health_status: str = "skipped"
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


def _default_migration_command() -> str:
    return "npx prisma migrate deploy --config prisma.config.ts"


def _default_migration_schedule_name() -> str:
    return "ver-apply-prisma-migrations"


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
    migration_status: str,
    migration_detail: str,
    health_result: VeriReelRolloutVerificationResult | None,
) -> PromotionRecord:
    deploy_mode = _default_deploy_mode()
    if deployment_record is not None:
        deploy_mode = deployment_record.deploy.deploy_mode
    destination_health = _build_destination_health(
        request=request,
        health_result=health_result,
    )
    post_deploy_update = _build_post_deploy_update(
        instance_name=request.to_instance,
        migration_status=migration_status,
        migration_detail=migration_detail,
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
        post_deploy_update=post_deploy_update,
        destination_health=destination_health,
    )


def _build_post_deploy_update(
    *,
    instance_name: str,
    migration_status: str,
    migration_detail: str,
) -> PostDeployUpdateEvidence:
    if migration_status == "skipped":
        return PostDeployUpdateEvidence(
            attempted=False,
            status="skipped",
            detail=migration_detail,
        )
    detail = migration_detail
    if not detail:
        if migration_status == "pass":
            detail = f"Prisma migrations completed on {instance_name}."
        elif migration_status == "fail":
            detail = f"Prisma migrations failed on {instance_name}."
    return PostDeployUpdateEvidence(
        attempted=True,
        status=migration_status,
        detail=detail,
    )


def _build_destination_health(
    *,
    request: VeriReelProdPromotionRequest,
    health_result: VeriReelRolloutVerificationResult | None,
) -> HealthcheckEvidence:
    if health_result is None:
        return HealthcheckEvidence(status="skipped")
    if not health_result.health_urls:
        return HealthcheckEvidence(status=health_result.status)
    return HealthcheckEvidence(
        verified=True,
        urls=health_result.health_urls,
        timeout_seconds=request.rollout_timeout_seconds,
        status=health_result.status,
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
    migration_status: str = "skipped",
    migration_detail: str = "",
    health_result: VeriReelRolloutVerificationResult | None = None,
) -> None:
    resolved_target_name = target_name or _default_target_name()
    resolved_target_type = target_type or _default_target_type()
    destination_health = _build_destination_health(
        request=request,
        health_result=health_result,
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
            post_deploy_update=_build_post_deploy_update(
                instance_name=request.to_instance,
                migration_status=migration_status,
                migration_detail=(migration_detail or error_message),
            ),
            destination_health=destination_health,
        )
    )


def _resolve_rollout_base_urls(
    *,
    control_plane_root: Path,
    request: VeriReelProdPromotionRequest,
) -> tuple[str, ...]:
    return resolve_verireel_rollout_base_urls(
        control_plane_root=control_plane_root,
        context=request.context,
        instance=request.to_instance,
    )


def _verify_rollout(
    *,
    control_plane_root: Path,
    request: VeriReelProdPromotionRequest,
) -> VeriReelRolloutVerificationResult:
    return verify_verireel_rollout(
        control_plane_root=control_plane_root,
        context=request.context,
        instance=request.to_instance,
        expected_build_revision=request.expected_build_revision,
        expected_build_tag=request.expected_build_tag,
        timeout_seconds=request.rollout_timeout_seconds,
        interval_seconds=request.rollout_interval_seconds,
        error_prefix="VeriReel prod rollout",
    )


def _resolve_application_id(
    *,
    control_plane_root: Path,
    request: VeriReelProdPromotionRequest,
    target_type: str,
    target_id: str,
) -> str:
    if target_type == "application" and target_id.strip():
        return target_id.strip()
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=request.context,
        instance_name=request.to_instance,
    )
    if target_definition is None or target_definition.target_type != "application" or not target_definition.target_id.strip():
        raise click.ClickException(
            f"VeriReel prod promotion post-deploy update requires an application target for {request.context}/{request.to_instance}."
        )
    return target_definition.target_id.strip()


def _find_application_schedule(*, host: str, token: str, application_id: str, schedule_name: str) -> dict[str, object] | None:
    for schedule in control_plane_dokploy.list_dokploy_schedules(
        host=host,
        token=token,
        target_id=application_id,
        schedule_type="application",
    ):
        if str(schedule.get("name") or "").strip() == schedule_name:
            return dict(schedule)
    return None


def _upsert_application_schedule(
    *,
    host: str,
    token: str,
    application_id: str,
    schedule_name: str,
    command: str,
) -> str:
    existing_schedule = _find_application_schedule(
        host=host,
        token=token,
        application_id=application_id,
        schedule_name=schedule_name,
    )
    payload: dict[str, object] = {
        "name": schedule_name,
        "cronExpression": control_plane_dokploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "scheduleType": "application",
        "shellType": "sh",
        "command": command,
        "applicationId": application_id,
        "enabled": False,
        "timezone": "UTC",
    }
    if existing_schedule is None:
        control_plane_dokploy.dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.create",
            method="POST",
            payload=payload,
        )
    else:
        control_plane_dokploy.dokploy_request(
            host=host,
            token=token,
            path="/api/schedule.update",
            method="POST",
            payload={"scheduleId": control_plane_dokploy.schedule_key(existing_schedule), **payload},
        )
    resolved_schedule = _find_application_schedule(
        host=host,
        token=token,
        application_id=application_id,
        schedule_name=schedule_name,
    )
    if resolved_schedule is None:
        raise click.ClickException(
            f"Dokploy schedule {schedule_name!r} for application {application_id!r} could not be resolved."
        )
    schedule_id = control_plane_dokploy.schedule_key(resolved_schedule)
    if not schedule_id:
        raise click.ClickException(
            f"Dokploy schedule {schedule_name!r} for application {application_id!r} did not expose a schedule id."
        )
    return schedule_id


def _run_application_command_with_retries(
    *,
    host: str,
    token: str,
    application_id: str,
    schedule_name: str,
    command: str,
    timeout_seconds: int,
    attempts: int = 4,
    retry_delay_seconds: float = 5.0,
) -> None:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    for attempt in range(1, attempts + 1):
        try:
            schedule_id = _upsert_application_schedule(
                host=host,
                token=token,
                application_id=application_id,
                schedule_name=schedule_name,
                command=command,
            )
            latest_before = control_plane_dokploy.latest_deployment_for_schedule(
                host=host,
                token=token,
                schedule_id=schedule_id,
            )
            control_plane_dokploy.dokploy_request(
                host=host,
                token=token,
                path="/api/schedule.runManually",
                method="POST",
                payload={"scheduleId": schedule_id},
                timeout_seconds=timeout_seconds,
            )
            control_plane_dokploy.wait_for_dokploy_schedule_deployment(
                host=host,
                token=token,
                schedule_id=schedule_id,
                before_key=control_plane_dokploy.deployment_key(latest_before),
                timeout_seconds=timeout_seconds,
            )
            return
        except click.ClickException:
            if attempt >= attempts:
                raise
            time.sleep(retry_delay_seconds)


def _run_prisma_migrations(
    *,
    control_plane_root: Path,
    request: VeriReelProdPromotionRequest,
    target_type: str,
    target_id: str,
) -> None:
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    application_id = _resolve_application_id(
        control_plane_root=control_plane_root,
        request=request,
        target_type=target_type,
        target_id=target_id,
    )
    _run_application_command_with_retries(
        host=host,
        token=token,
        application_id=application_id,
        schedule_name=_default_migration_schedule_name(),
        command=_default_migration_command(),
        timeout_seconds=request.rollout_timeout_seconds,
    )


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
            migration_status="skipped",
            health_status="skipped",
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
            expected_build_revision=request.expected_build_revision,
            expected_build_tag=request.expected_build_tag,
            rollout_timeout_seconds=request.rollout_timeout_seconds,
            rollout_interval_seconds=request.rollout_interval_seconds,
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
            migration_status="skipped",
            migration_detail="",
            health_result=None,
        )
        record_store.write_promotion_record(promotion_record)
        return VeriReelProdPromotionResult(
            promotion_record_id=request.promotion_record_id,
            deployment_record_id=deployment_result.deployment_record_id,
            backup_record_id=backup_gate_record.record_id,
            deploy_status=deployment_result.deploy_status,
            rollout_status="skipped",
            migration_status="skipped",
            health_status="skipped",
            deploy_started_at=deployment_result.deploy_started_at,
            deploy_finished_at=deployment_result.deploy_finished_at,
            target_name=deployment_result.target_name,
            target_type=deployment_result.target_type,
            target_id=deployment_result.target_id,
            error_message=deployment_result.error_message,
        )

    if deployment_result.rollout_status != "pass":
        error_message = deployment_result.error_message or "VeriReel prod rollout verification failed."
        failed_rollout_result = VeriReelRolloutVerificationResult(
            status="fail",
            base_url=deployment_result.rollout_base_url,
            health_urls=deployment_result.rollout_health_urls,
        )
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
            health_result=failed_rollout_result,
        )
        return VeriReelProdPromotionResult(
            promotion_record_id=request.promotion_record_id,
            deployment_record_id=deployment_result.deployment_record_id,
            backup_record_id=backup_gate_record.record_id,
            deploy_status=deployment_result.deploy_status,
            rollout_status="fail",
            migration_status="skipped",
            health_status="skipped",
            deploy_started_at=deployment_result.deploy_started_at,
            deploy_finished_at=deployment_result.deploy_finished_at,
            target_name=deployment_result.target_name,
            target_type=deployment_result.target_type,
            target_id=deployment_result.target_id,
            error_message=error_message,
        )
    rollout_result = VeriReelRolloutVerificationResult(
        status=deployment_result.rollout_status,
        base_url=deployment_result.rollout_base_url,
        health_urls=deployment_result.rollout_health_urls,
        started_at=deployment_result.rollout_started_at,
        finished_at=deployment_result.rollout_finished_at,
    )

    try:
        _run_prisma_migrations(
            control_plane_root=control_plane_root,
            request=request,
            target_type=deployment_result.target_type,
            target_id=deployment_result.target_id,
        )
    except click.ClickException as exc:
        error_message = str(exc)
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
            migration_status="fail",
            migration_detail=error_message,
            health_result=None,
        )
        return VeriReelProdPromotionResult(
            promotion_record_id=request.promotion_record_id,
            deployment_record_id=deployment_result.deployment_record_id,
            backup_record_id=backup_gate_record.record_id,
            deploy_status=deployment_result.deploy_status,
            rollout_status=rollout_result.status,
            migration_status="fail",
            health_status="skipped",
            deploy_started_at=deployment_result.deploy_started_at,
            deploy_finished_at=deployment_result.deploy_finished_at,
            target_name=deployment_result.target_name,
            target_type=deployment_result.target_type,
            target_id=deployment_result.target_id,
            error_message=error_message,
        )

    try:
        health_result = _verify_rollout(
            control_plane_root=control_plane_root,
            request=request,
        )
    except click.ClickException as exc:
        error_message = str(exc)
        failed_health_result = VeriReelRolloutVerificationResult(status="fail")
        try:
            base_urls = _resolve_rollout_base_urls(
                control_plane_root=control_plane_root,
                request=request,
            )
            failed_health_result = VeriReelRolloutVerificationResult(
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
            migration_status="pass",
            migration_detail="",
            health_result=failed_health_result,
        )
        return VeriReelProdPromotionResult(
            promotion_record_id=request.promotion_record_id,
            deployment_record_id=deployment_result.deployment_record_id,
            backup_record_id=backup_gate_record.record_id,
            deploy_status=deployment_result.deploy_status,
            rollout_status=rollout_result.status,
            migration_status="pass",
            health_status="fail",
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
        migration_status="pass",
        migration_detail="",
        health_result=health_result,
    )
    record_store.write_promotion_record(promotion_record)
    return VeriReelProdPromotionResult(
        promotion_record_id=request.promotion_record_id,
        deployment_record_id=deployment_result.deployment_record_id,
        backup_record_id=backup_gate_record.record_id,
        deploy_status=deployment_result.deploy_status,
        rollout_status=rollout_result.status,
        migration_status="pass",
        health_status=health_result.status,
        deploy_started_at=deployment_result.deploy_started_at,
        deploy_finished_at=deployment_result.deploy_finished_at,
        target_name=deployment_result.target_name,
        target_type=deployment_result.target_type,
        target_id=deployment_result.target_id,
        error_message=deployment_result.error_message,
    )
