from datetime import UTC, datetime

from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    PromotionRequest,
    PromotionRecord,
)


def build_promotion_record(
    *,
    record_id: str,
    artifact_id: str,
    context_name: str,
    from_instance_name: str,
    to_instance_name: str,
    target_name: str,
    target_type: str,
    deploy_mode: str,
    deployment_id: str = "",
    source_health: HealthcheckEvidence | None = None,
    backup_gate: BackupGateEvidence | None = None,
    destination_health: HealthcheckEvidence | None = None,
) -> PromotionRecord:
    return PromotionRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id=artifact_id),
        context=context_name,
        from_instance=from_instance_name,
        to_instance=to_instance_name,
        source_health=source_health or HealthcheckEvidence(),
        backup_gate=backup_gate or BackupGateEvidence(),
        deploy=DeploymentEvidence(
            target_name=target_name,
            target_type=target_type,
            deploy_mode=deploy_mode,
            deployment_id=deployment_id,
        ),
        post_deploy_update=PostDeployUpdateEvidence(),
        destination_health=destination_health or HealthcheckEvidence(),
    )


def generate_promotion_record_id(*, context_name: str, from_instance_name: str, to_instance_name: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"promotion-{timestamp}-{context_name}-{from_instance_name}-to-{to_instance_name}"


def _resolve_post_deploy_update(status_target_type: str, *, wait: bool, deployment_status: str) -> PostDeployUpdateEvidence:
    if not wait or status_target_type != "compose":
        return PostDeployUpdateEvidence()
    if deployment_status == "pending":
        return PostDeployUpdateEvidence(
            attempted=True,
            status="pending",
            detail="Post-deploy update workflow is pending.",
        )
    return PostDeployUpdateEvidence(
        attempted=True,
        status="pass" if deployment_status == "pass" else deployment_status,
        detail="Post-deploy update workflow completed."
        if deployment_status == "pass"
        else "Ship workflow did not complete successfully.",
    )


def _resolve_destination_health(
    destination_health: HealthcheckEvidence,
    *,
    wait: bool,
    deployment_status: str,
) -> HealthcheckEvidence:
    if destination_health.status == "skipped":
        return destination_health
    if not wait:
        return HealthcheckEvidence(
            verified=False,
            urls=destination_health.urls,
            timeout_seconds=destination_health.timeout_seconds,
            status="pending",
        )
    if deployment_status == "pass":
        return HealthcheckEvidence(
            verified=True,
            urls=destination_health.urls,
            timeout_seconds=destination_health.timeout_seconds,
            status="pass",
        )
    return HealthcheckEvidence(
        verified=False,
        urls=destination_health.urls,
        timeout_seconds=destination_health.timeout_seconds,
        status="fail",
    )


def build_executed_promotion_record(
    *,
    request: PromotionRequest,
    record_id: str,
    deployment_id: str,
    deployment_status: str,
) -> PromotionRecord:
    return PromotionRecord(
        record_id=record_id,
        artifact_identity=ArtifactIdentityReference(artifact_id=request.artifact_id),
        context=request.context,
        from_instance=request.from_instance,
        to_instance=request.to_instance,
        source_health=request.source_health,
        backup_gate=request.backup_gate,
        deploy=DeploymentEvidence(
            target_name=request.target_name,
            target_type=request.target_type,
            deploy_mode=request.deploy_mode,
            deployment_id=deployment_id,
            status=deployment_status,
        ),
        post_deploy_update=_resolve_post_deploy_update(
            request.target_type,
            wait=request.wait,
            deployment_status=deployment_status,
        ),
        destination_health=_resolve_destination_health(
            request.destination_health,
            wait=request.wait,
            deployment_status=deployment_status,
        ),
    )
