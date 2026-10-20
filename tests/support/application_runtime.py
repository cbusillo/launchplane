"""Application provider fixtures with independent process convergence evidence."""

from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.dokploy.application_runtime import ApplicationRuntimeObservation
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebResolvedDeployTarget,
)
from control_plane.workflows.dokploy_deploy import dokploy_deploy_artifact_reference


def converged_application_runtime(
    target: GenericWebResolvedDeployTarget, identity: RuntimeIdentity
) -> ApplicationRuntimeObservation:
    return ApplicationRuntimeObservation(
        application_id=target.resolved_target.target_id,
        app_name=target.resolved_target.target_name,
        service_id="fixture-service",
        service_image=dokploy_deploy_artifact_reference(
            ship_request=target.ship_request, target_type="application"
        ),
        service_deployment_record_id=identity.deployment_record_id,
        desired_replicas=1,
        rollout_state="completed",
        rollout_started_at="2026-07-13T12:00:00Z",
        running_container_images=((identity.artifact_id,),),
        running_container_deployment_record_ids=(identity.deployment_record_id,),
        running_container_health=("healthy",),
        pending_task_count=0,
    )


def recovered_application_runtime(
    target: GenericWebResolvedDeployTarget, deployment_title: str
) -> ApplicationRuntimeObservation:
    # Both the original runtime identity and the provider title carry the same
    # operation hash. Model the identity the interrupted original deploy wrote.
    digest = deployment_title.removeprefix("Launchplane operation ")
    request = target.ship_request
    return converged_application_runtime(
        target,
        RuntimeIdentity(
            context=request.context,
            instance=request.instance,
            artifact_id=request.artifact_id,
            source_git_ref=request.source_git_ref,
            deployment_record_id=(
                f"deployment-provider-operation-{digest}-{request.context}-{request.instance}"
            ),
        ),
    )
