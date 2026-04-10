from datetime import UTC, datetime

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.deployment_record import DelegatedExecutor
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.promotion_record import ArtifactIdentityReference, DeploymentEvidence, HealthcheckEvidence
from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.contracts.ship_request import CompatibilityShipRequest


def generate_deployment_record_id(*, context_name: str, instance_name: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"deployment-{timestamp}-{context_name}-{instance_name}"


def utc_now_timestamp() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _resolve_post_deploy_update(
    request: CompatibilityShipRequest,
    *,
    deployment_status: str,
) -> PostDeployUpdateEvidence:
    if not request.wait or request.target_type != "compose":
        return PostDeployUpdateEvidence()
    if deployment_status == "pending":
        return PostDeployUpdateEvidence(
            attempted=True,
            status="pending",
            detail=(
                "Odoo-specific post-deploy update is pending through the canonical "
                "odoo-ai platform update workflow."
            ),
        )
    if deployment_status == "pass":
        return PostDeployUpdateEvidence(
            attempted=True,
            status="pass",
            detail=(
                "Odoo-specific post-deploy update completed through the canonical "
                "odoo-ai platform update workflow."
            ),
        )
    return PostDeployUpdateEvidence(
        attempted=False,
        status="skipped",
        detail="Odoo-specific post-deploy update did not run because deploy execution did not complete successfully.",
    )


def build_compatibility_deployment_record(
    *,
    request: CompatibilityShipRequest,
    record_id: str,
    deployment_id: str,
    deployment_status: str,
    started_at: str,
    finished_at: str,
    resolved_target: ResolvedTargetEvidence | None = None,
    delegated_executor: DelegatedExecutor = "control-plane.dokploy",
    post_deploy_update: PostDeployUpdateEvidence | None = None,
    destination_health: HealthcheckEvidence | None = None,
) -> DeploymentRecord:
    artifact_identity = None
    if request.artifact_id.strip():
        artifact_identity = ArtifactIdentityReference(artifact_id=request.artifact_id)

    return DeploymentRecord(
        record_id=record_id,
        artifact_identity=artifact_identity,
        context=request.context,
        instance=request.instance,
        source_git_ref=request.source_git_ref,
        wait_for_completion=request.wait,
        verify_destination_health=request.verify_health,
        no_cache=request.no_cache,
        delegated_executor=delegated_executor,
        branch_sync=request.branch_sync,
        resolved_target=resolved_target,
        deploy=DeploymentEvidence(
            target_name=request.target_name,
            target_type=request.target_type,
            deploy_mode=request.deploy_mode,
            deployment_id=deployment_id,
            status=deployment_status,
            started_at=started_at,
            finished_at=finished_at,
        ),
        post_deploy_update=post_deploy_update or _resolve_post_deploy_update(
            request,
            deployment_status=deployment_status,
        ),
        destination_health=destination_health
        or _resolve_destination_health(
            request.destination_health,
            wait=request.wait,
            deployment_status=deployment_status,
        ),
    )
