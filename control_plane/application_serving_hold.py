from control_plane.application_state_volume import ApplicationStateVolumeAdapter
from control_plane.contracts.deployment_record import DeploymentRecord, deployment_record_passed
from control_plane.contracts.deploy_reference import (
    is_digest_pinned_image_reference,
    provider_image_reference,
)
from control_plane.dokploy.fresh_application import require_fresh_application_runtime
from control_plane.dokploy.api import JsonObject


def current_application_deployment(adapter: ApplicationStateVolumeAdapter) -> DeploymentRecord:
    store, context = adapter.store, adapter.request.context
    inventory = store.read_environment_inventory(context_name=context, instance_name="testing")
    deployment = store.read_deployment_record(inventory.deployment_record_id)
    provider = store.read_provider_target_record(context_name=context, instance_name="testing")
    if (
        inventory.promotion_record_id
        or inventory.promoted_from_instance
        or not deployment_record_passed(deployment)
        or inventory.deploy != deployment.deploy
        or inventory.deploy.status != "pass"
        or not deployment.deploy.deployment_id
        or deployment.deployed_target != provider.to_deployed_target_reference()
        or (deployment.context, deployment.instance) != (context, "testing")
        or deployment.artifact_identity is None
        or inventory.artifact_identity != deployment.artifact_identity
        or inventory.source_git_ref != deployment.source_git_ref
        or inventory.runtime_identity != deployment.runtime_identity
        or deployment.runtime_identity is None
        or deployment.runtime_identity.instance != "testing"
        or deployment.runtime_identity.context != context
        or deployment.runtime_identity.deployment_record_id != deployment.record_id
        or deployment.runtime_identity.artifact_id != deployment.artifact_identity.artifact_id
        or not is_digest_pinned_image_reference(deployment.artifact_identity.artifact_id)
    ):
        raise ValueError("Serving continuation requires its exact successful current deployment.")
    return deployment


def require_serving_application_provider(
    adapter: ApplicationStateVolumeAdapter, live: JsonObject, deployment: DeploymentRecord
) -> None:
    import json

    phase = json.loads(adapter._progress_phase())
    assert deployment.artifact_identity is not None
    image = provider_image_reference(
        artifact_id=deployment.artifact_identity.artifact_id,
        deploy_reference=deployment.runtime_identity.image_reference
        if deployment.runtime_identity
        else "",
    )
    history = live.get("deployments")
    if (
        live.get("sourceType") != "docker"
        or live.get("dockerImage") != image
        or not isinstance(live.get("env"), str)
        or live.get("applicationStatus") != "done"
        or not isinstance(history, list)
        or not history
        or any(
            not isinstance(row, dict) or row.get("status") not in {"done", "error"}
            for row in history
        )
        or not any(
            isinstance(row, dict)
            and row.get("deploymentId") == deployment.deploy.deployment_id
            and row.get("status") == "done"
            for row in history
        )
        or live.get("endpointSpecSwarm") not in (None, {"Mode": "dnsrr"})
    ):
        raise ValueError("The serving application differs from its completed deployment.")
    require_fresh_application_runtime(
        {
            **live,
            "sourceType": None,
            "dockerImage": None,
            "env": None,
            "deployments": [],
            "applicationStatus": "idle",
            "endpointSpecSwarm": None,
        },
        environment_id=phase["environment"],
        project_id=phase["project"],
    )
