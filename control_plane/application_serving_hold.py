import hmac
import json

from control_plane.application_state_volume import ApplicationStateVolumeAdapter
from control_plane.contracts.deployment_record import DeploymentRecord, deployment_record_passed
from control_plane.contracts.deploy_reference import (
    is_digest_pinned_image_reference,
    provider_image_reference,
)
from control_plane.dokploy.fresh_application import require_fresh_application_runtime
from control_plane.dokploy.api import JsonObject
from control_plane.dokploy.api import parse_dokploy_env_text
from control_plane.dokploy import source
from control_plane.dokploy.post_deploy import (
    DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES,
    DOKPLOY_CANCELLED_DEPLOYMENT_STATUSES,
)
from control_plane.dokploy.application_runtime import (
    fetch_application_runtime,
    application_runtime_matches,
)
from control_plane.generic_web_testing_inputs import prepare_testing_inputs, testing_input_authority
from control_plane.runtime_platform_credentials import (
    looks_like_credential,
    refuse_platform_credentials,
)


def require_managed_serving_credentials(
    adapter: ApplicationStateVolumeAdapter, values: dict[str, str]
) -> None:
    profile = adapter.store.read_product_profile_record(adapter.product)
    lane = next(
        lane
        for lane in profile.lanes
        if (lane.context, lane.instance) == (adapter.request.context, "testing")
    )
    authority = testing_input_authority(adapter.store, profile, lane)
    managed = prepare_testing_inputs(adapter.store, profile, lane, authority).environment.values
    refuse_platform_credentials(
        managed, target="testing application", source="managed testing inputs"
    )
    refuse_platform_credentials(values, target="testing application", source="provider runtime")
    if values.get("DOCKER_HOST") or any(
        looks_like_credential(key, value)
        and not hmac.compare_digest(value.encode(), managed.get(key, "").encode())
        for key, value in values.items()
        if value
    ):
        raise ValueError("Serving credentials have no current managed testing supplier.")


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
    for other in store.list_deployment_records(context_name=context, instance_name="testing"):
        if (
            other.record_id == deployment.record_id
            or other.deployed_target is not None
            and other.deployed_target.target_id != provider.target_id
        ):
            continue
        if other.deploy.status == "pending" or (
            other.deploy.started_at and other.deploy.started_at > deployment.deploy.started_at
        ):
            raise ValueError("Another deployment has unresolved or newer serving authority.")
    return deployment


def require_serving_application_provider(
    adapter: ApplicationStateVolumeAdapter,
    live: JsonObject,
    deployment: DeploymentRecord,
    runtime_binding: dict[str, object],
) -> None:
    phase = json.loads(adapter._progress_phase())
    if deployment.artifact_identity is None:
        raise ValueError("Serving artifact identity is unavailable.")
    image = provider_image_reference(
        artifact_id=deployment.artifact_identity.artifact_id,
        deploy_reference=deployment.runtime_identity.image_reference
        if deployment.runtime_identity
        else "",
    )
    history = live.get("deployments")
    terminal_statuses = (
        DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES | DOKPLOY_CANCELLED_DEPLOYMENT_STATUSES | {"error"}
    )
    if (
        live.get("sourceType") != "docker"
        or live.get("dockerImage") != image
        or not isinstance(live.get("env"), str)
        or live.get("applicationStatus") != "done"
        or not isinstance(history, list)
        or not history
        or not isinstance(history[0], dict)
        or history[0].get("deploymentId") != deployment.deploy.deployment_id
        or history[0].get("status") not in DOKPLOY_SUCCESS_DEPLOYMENT_STATUSES
        or any(
            not isinstance(row, dict) or row.get("status") not in terminal_statuses
            for row in history
        )
        or live.get("endpointSpecSwarm") not in (None, {"Mode": "dnsrr"})
    ):
        raise ValueError("The serving application differs from its completed deployment.")
    values = parse_dokploy_env_text(str(live["env"]))
    require_managed_serving_credentials(adapter, values)
    host, token = source.read_dokploy_config(control_plane_root=adapter.root)
    observed = fetch_application_runtime(
        host=host,
        token=token,
        application_id=str(live["applicationId"]),
        expected_environment=values,
    )
    if (
        observed.application_id != live["applicationId"]
        or observed.app_name != live["appName"]
        or observed.server_id != adapter.request.server_id
        or observed.desired_replicas != 1
        or observed.service_version < 1
        or not application_runtime_matches(
            observed,
            artifact_reference=deployment.artifact_identity.artifact_id,
            provider_reference=image,
            deployment_record_id=deployment.record_id,
        )
    ):
        raise ValueError("The current serving runtime is unproved.")
    binding: dict[str, object] = {
        "service_id": observed.service_id,
        "service_version": observed.service_version,
        "container_ids": sorted(item.container_id for item in observed.running_containers),
    }
    if runtime_binding and runtime_binding != binding:
        raise ValueError("The serving runtime changed during qualification.")
    runtime_binding.update(binding)
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
