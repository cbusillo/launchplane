from __future__ import annotations

from pathlib import Path

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
) -> PromotionRecord:
    deploy_mode = _default_deploy_mode()
    if deployment_record is not None:
        deploy_mode = deployment_record.deploy.deploy_mode
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
        destination_health=HealthcheckEvidence(status="skipped"),
    )


def _write_failed_promotion_record(
    *,
    record_store: FilesystemRecordStore,
    request: VeriReelProdPromotionRequest,
    backup_gate_record: BackupGateRecord | None,
    error_message: str,
) -> None:
    record_store.write_promotion_record(
        PromotionRecord(
            record_id=request.promotion_record_id,
            artifact_identity=ArtifactIdentityReference(artifact_id=request.artifact_id),
            deployment_record_id="",
            backup_record_id=request.backup_record_id,
            context=request.context,
            from_instance=request.from_instance,
            to_instance=request.to_instance,
            source_health=HealthcheckEvidence(status="skipped"),
            backup_gate=BackupGateEvidence(
                required=True,
                status="fail",
                evidence=(
                    dict(backup_gate_record.evidence)
                    if backup_gate_record is not None
                    else {"backup_record_id": request.backup_record_id, "error": error_message}
                ),
            ),
            deploy=DeploymentEvidence(
                target_name=_default_target_name(),
                target_type=_default_target_type(),
                deploy_mode=_default_deploy_mode(),
                deployment_id="",
                status="fail",
            ),
            post_deploy_update=PostDeployUpdateEvidence(
                attempted=False,
                status="skipped",
                detail=error_message,
            ),
            destination_health=HealthcheckEvidence(status="skipped"),
        )
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
    )
    record_store.write_promotion_record(promotion_record)
    return VeriReelProdPromotionResult(
        promotion_record_id=request.promotion_record_id,
        deployment_record_id=deployment_result.deployment_record_id,
        backup_record_id=backup_gate_record.record_id,
        deploy_status=deployment_result.deploy_status,
        deploy_started_at=deployment_result.deploy_started_at,
        deploy_finished_at=deployment_result.deploy_finished_at,
        target_name=deployment_result.target_name,
        target_type=deployment_result.target_type,
        target_id=deployment_result.target_id,
        error_message=deployment_result.error_message,
    )
