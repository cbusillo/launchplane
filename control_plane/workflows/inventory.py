from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory


def inventory_record_id(*, context_name: str, instance_name: str) -> str:
    return f"{context_name}-{instance_name}"


def build_environment_inventory(
    *,
    deployment_record: DeploymentRecord,
    updated_at: str,
    promotion_record_id: str = "",
    promoted_from_instance: str = "",
) -> EnvironmentInventory:
    return EnvironmentInventory(
        context=deployment_record.context,
        instance=deployment_record.instance,
        artifact_identity=deployment_record.artifact_identity,
        source_git_ref=deployment_record.source_git_ref,
        deploy=deployment_record.deploy,
        post_deploy_update=deployment_record.post_deploy_update,
        destination_health=deployment_record.destination_health,
        updated_at=updated_at,
        deployment_record_id=deployment_record.record_id,
        promotion_record_id=promotion_record_id,
        promoted_from_instance=promoted_from_instance,
    )
