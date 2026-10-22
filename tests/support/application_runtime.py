"""Application provider fixtures with independent process convergence evidence."""

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from unittest.mock import patch

from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.dokploy.application_runtime import ApplicationRuntimeObservation
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebResolvedDeployTarget,
    DokployGenericWebDeployProvider,
)
from control_plane.workflows.dokploy_deploy import dokploy_deploy_artifact_reference
from control_plane.workflows import generic_web_application_snapshot


class ApplicationRuntimeTransport:
    def __init__(self, *, application_id: str, identity: RuntimeIdentity) -> None:
        self.target: dict[str, Any] = {
            "applicationId": application_id,
            "appName": "runtime-fixture",
            "serverId": "fixture-server",
            "sourceType": "docker",
            "dockerImage": identity.image_reference,
        }
        self.service: dict[str, Any] = {
            "ID": "fixture-service",
            "Version": {"Index": 2},
            "Spec": {
                "Name": self.target["appName"],
                "Mode": {"Replicated": {"Replicas": 1}},
                "TaskTemplate": {
                    "ContainerSpec": {
                        "Image": identity.image_reference,
                        "Env": [
                            "LAUNCHPLANE_DEPLOYMENT_RECORD_ID=" + identity.deployment_record_id,
                            "FIXTURE_SECRET=private-fixture-value",
                        ],
                    }
                },
            },
            "UpdateStatus": {
                "State": "completed",
                "StartedAt": "2026-10-20T03:08:20Z",
                "CompletedAt": "2026-10-20T03:08:28Z",
            },
        }
        self.container_id = "c" * 64
        self.image_id = "sha256:" + "d" * 64
        self.task: dict[str, Any] = {
            "ID": "fixture-task",
            "ServiceID": self.service["ID"],
            "NodeID": "fixture-node",
            "DesiredState": "running",
            "Status": {
                "State": "running",
                "ContainerStatus": {"ContainerID": self.container_id},
            },
            "Spec": {"ContainerSpec": {"Image": identity.image_reference}},
        }
        self.container: dict[str, Any] = {
            "Id": self.container_id,
            "Image": self.image_id,
            "Config": {
                "Image": identity.image_reference,
                "Env": list(self.service["Spec"]["TaskTemplate"]["ContainerSpec"]["Env"]),
                "Labels": {
                    "com.docker.swarm.service.id": self.service["ID"],
                    "com.docker.swarm.service.name": self.target["appName"],
                    "com.docker.swarm.task.id": self.task["ID"],
                    "com.docker.swarm.node.id": self.task["NodeID"],
                },
            },
            "State": {
                "Running": True,
                "Paused": False,
                "Restarting": False,
                "Health": {"Status": "healthy"},
            },
        }
        self.image: dict[str, Any] = {"Id": self.image_id, "RepoDigests": [identity.artifact_id]}

    def target_payload(self, **kwargs: Any) -> dict[str, Any]:
        if (
            kwargs["target_type"] != "application"
            or kwargs["target_id"] != self.target["applicationId"]
        ):
            raise AssertionError("Unexpected provider target read.")
        return deepcopy(self.target)

    def request(self, **kwargs: Any) -> object:
        query = kwargs["query"]
        if query.get("serverId") != self.target["serverId"]:
            raise AssertionError("Unexpected runtime server.")
        if kwargs["path"] == "/api/docker.getServiceContainersByAppName":
            if query["appName"] != self.target["appName"]:
                raise AssertionError("Unexpected runtime application.")
            return [{"containerId": self.task["ID"], "state": "running"}]
        if kwargs["path"] != "/api/docker.getConfig":
            raise AssertionError("Only runtime metadata reads are allowed.")
        return deepcopy(
            {
                self.target["appName"]: self.service,
                self.task["ID"]: self.task,
                self.container_id: self.container,
                self.image_id.removeprefix("sha256:"): self.image,
            }[query["containerId"]]
        )

    @contextmanager
    def reads(self) -> Iterator[None]:
        with (
            patch.object(
                DokployGenericWebDeployProvider,
                "_read_provider_config",
                return_value=("https://provider.example", "fixture-token"),
            ),
            patch("control_plane.dokploy.api.fetch_dokploy_target_payload", self.target_payload),
            patch("control_plane.dokploy.api.dokploy_request", self.request),
        ):
            yield


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
    target: GenericWebResolvedDeployTarget, provider_operation_key: str
) -> ApplicationRuntimeObservation:
    request = target.ship_request
    return converged_application_runtime(
        target,
        RuntimeIdentity(
            context=request.context,
            instance=request.instance,
            artifact_id=request.artifact_id,
            source_git_ref=request.source_git_ref,
            deployment_record_id=generic_web_application_snapshot.generic_web_provider_deployment_record_id(
                provider_operation_key, context=request.context, instance=request.instance
            ),
        ),
    )
