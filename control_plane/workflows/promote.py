from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BackupGateEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
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
        destination_health=destination_health or HealthcheckEvidence(),
    )
