"""Read exact Swarm application runtime facts without retaining environment values."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re

import click

from control_plane.contracts.deploy_reference import docker_image_digest, docker_image_repository
from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp
from control_plane.dokploy import api
from control_plane.dokploy.runtime_evidence import DokployEvidenceProviderError

_IDENTIFIER = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")
_CONTAINER_ID = re.compile(r"^[a-f0-9]{64}$")
_IMAGE_ID = re.compile(r"^sha256:[a-f0-9]{64}$")
_ROLLOUT_STATES = frozenset(
    {"updating", "paused", "completed", "rollback_started", "rollback_paused", "rollback_completed"}
)
_TASK_STATES = frozenset(
    {
        "new",
        "allocated",
        "pending",
        "assigned",
        "accepted",
        "preparing",
        "ready",
        "starting",
        "running",
        "complete",
        "shutdown",
        "failed",
        "rejected",
        "remove",
        "orphaned",
    }
)
_DEPLOYMENT_PREFIX = "LAUNCHPLANE_DEPLOYMENT_RECORD_ID="


@dataclass(frozen=True)
class ApplicationRuntimeObservation:
    application_id: str
    app_name: str
    server_id: str
    service_id: str
    service_version: int
    service_image: str
    service_deployment_record_id: str
    service_environment_keys: tuple[str, ...]
    desired_replicas: int
    rollout_state: str
    rollout_started_at: str
    rollout_completed_at: str
    running_container_images: tuple[str, ...]
    running_container_deployment_record_ids: tuple[str, ...]
    running_container_health: tuple[str, ...]
    pending_task_count: int

    @property
    def ready(self) -> bool:
        """Process convergence only; callers still prove their expected artifact and health."""
        return (
            self.desired_replicas > 0
            and self.rollout_state in {"", "completed"}
            and self.pending_task_count == 0
            and len(self.running_container_images) == self.desired_replicas
            and all(state in {"", "healthy"} for state in self.running_container_health)
            and bool(self.service_deployment_record_id)
            and all(
                record_id == self.service_deployment_record_id
                for record_id in self.running_container_deployment_record_ids
            )
        )


def application_runtime_matches(
    observed: ApplicationRuntimeObservation,
    *,
    artifact_reference: str,
    provider_reference: str,
    deployment_record_id: str,
) -> bool:
    return (
        observed.ready
        and _service_image_matches(observed.service_image, artifact_reference, provider_reference)
        and observed.service_deployment_record_id == deployment_record_id
        and all(image == artifact_reference for image in observed.running_container_images)
        and all(
            record_id == deployment_record_id
            for record_id in observed.running_container_deployment_record_ids
        )
    )


def _service_image_matches(image: str, artifact_reference: str, provider_reference: str) -> bool:
    if "@" in image:
        return _repository(image) == _repository(artifact_reference) and docker_image_digest(
            image
        ) == docker_image_digest(artifact_reference)
    return image == provider_reference


def application_runtime_drift_fingerprint(
    observed: ApplicationRuntimeObservation,
    *,
    artifact_reference: str,
    provider_reference: str,
    deployment_record_id: str,
) -> str:
    """Only a stable, positively different service permits a deterministic repair."""
    if observed.rollout_state not in {"", "completed", "rollback_completed"}:
        return ""
    if (
        _service_image_matches(observed.service_image, artifact_reference, provider_reference)
        and observed.service_deployment_record_id == deployment_record_id
    ):
        return ""
    return hashlib.sha256(
        json.dumps(
            {
                "service_id": observed.service_id,
                "image": observed.service_image,
                "deployment_record_id": observed.service_deployment_record_id,
                "desired_replicas": observed.desired_replicas,
                "rollout_state": observed.rollout_state,
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise DokployEvidenceProviderError("container-config")
    return value


def _identifier(value: object) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise DokployEvidenceProviderError("container-identity")
    return value


def _environment(value: object) -> tuple[tuple[str, ...], str]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or "=" not in item for item in value
    ):
        raise DokployEvidenceProviderError("container-config")
    entries = [item for item in value if isinstance(item, str)]
    keys = [item.split("=", 1)[0] for item in entries]
    if len(keys) != len(set(keys)) or any(
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) for key in keys
    ):
        raise DokployEvidenceProviderError("container-config")
    deployment_ids = [
        item[len(_DEPLOYMENT_PREFIX) :] for item in entries if item.startswith(_DEPLOYMENT_PREFIX)
    ]
    deployment_id = deployment_ids[0] if deployment_ids else ""
    if len(deployment_id) > 500 or any(character.isspace() for character in deployment_id):
        raise DokployEvidenceProviderError("container-identity")
    return tuple(sorted(keys)), deployment_id


def _repository(reference: str) -> str:
    return docker_image_repository(reference.split("@", 1)[0])


def _image(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 500
        or any(c.isspace() for c in value)
    ):
        raise DokployEvidenceProviderError("image-identity")
    if not _repository(value) or "://" in value:
        raise DokployEvidenceProviderError("image-identity")
    return value


def fetch_application_runtime(
    *,
    host: str,
    token: str,
    application_id: str,
) -> ApplicationRuntimeObservation:
    """Inspect the recorded server's exact service, tasks, containers and image digests.

    Dokploy's ``getConfig`` uses generic Docker inspect, including service/task/image
    objects. ``getServiceContainersByAppName`` returns task IDs as ``containerId``.
    Raw provider responses never escape this reader, including environment values.
    """
    try:
        return _fetch_application_runtime(host=host, token=token, application_id=application_id)
    except click.ClickException:
        raise DokployEvidenceProviderError("target-inspect") from None


def _fetch_application_runtime(
    *,
    host: str,
    token: str,
    application_id: str,
) -> ApplicationRuntimeObservation:
    target = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="application",
        target_id=application_id,
    )
    if target.get("applicationId") != application_id or "serverId" not in target:
        raise DokployEvidenceProviderError("target-inspect")
    app_name = _identifier(target.get("appName"))
    raw_server = target.get("serverId")
    server_id = "" if raw_server is None or raw_server == "" else _identifier(raw_server)
    query: dict[str, str | int] = {"serverId": server_id} if server_id else {}

    def inspect(identifier: str) -> dict[str, object]:
        return _object(
            api.dokploy_request(
                host=host,
                token=token,
                path="/api/docker.getConfig",
                query={**query, "containerId": identifier},
            )
        )

    service = inspect(app_name)
    service_id = _identifier(service.get("ID"))
    version = _object(service.get("Version")).get("Index")
    if type(version) is not int or version < 1:
        raise DokployEvidenceProviderError("container-config")
    spec = _object(service.get("Spec"))
    if spec.get("Name") != app_name:
        raise DokployEvidenceProviderError("container-identity")
    container_spec = _object(_object(spec.get("TaskTemplate")).get("ContainerSpec"))
    service_image = _image(container_spec.get("Image"))
    environment_keys, deployment_id = _environment(container_spec.get("Env"))
    replicas = _object(_object(spec.get("Mode")).get("Replicated")).get("Replicas")
    if type(replicas) is not int or replicas < 0:
        raise DokployEvidenceProviderError("container-config")
    rollout = _object(service["UpdateStatus"]) if "UpdateStatus" in service else {}
    rollout_state = rollout.get("State", "")
    started_at = rollout.get("StartedAt", "")
    completed_at = rollout.get("CompletedAt", "")
    if (
        not isinstance(rollout_state, str)
        or ("UpdateStatus" in service and not rollout_state)
        or (rollout_state and rollout_state not in _ROLLOUT_STATES)
        or not isinstance(started_at, str)
        or len(started_at) > 100
        or not isinstance(completed_at, str)
        or len(completed_at) > 100
    ):
        raise DokployEvidenceProviderError("container-config")
    if rollout_state:
        try:
            started = parse_launchplane_mutation_timestamp(started_at, field_name="started_at")
            if rollout_state in {"completed", "rollback_completed"}:
                finished = parse_launchplane_mutation_timestamp(
                    completed_at, field_name="finished_at"
                )
                if finished < started:
                    raise ValueError("Rollout timestamps are out of order.")
        except ValueError:
            raise DokployEvidenceProviderError("container-config") from None
    tasks = api.dokploy_request(
        host=host,
        token=token,
        path="/api/docker.getServiceContainersByAppName",
        query={**query, "appName": app_name},
    )
    if not isinstance(tasks, list) or not tasks:
        raise DokployEvidenceProviderError("container-list")
    task_ids = [_identifier(_object(row).get("containerId")) for row in tasks]
    if len(task_ids) != len(set(task_ids)):
        raise DokployEvidenceProviderError("container-identity")
    images: list[str] = []
    record_ids: list[str] = []
    health: list[str] = []
    pending = 0
    containers: set[str] = set()
    for task_id in task_ids:
        task = inspect(task_id)
        if task.get("ID") != task_id or task.get("ServiceID") != service_id:
            raise DokployEvidenceProviderError("container-identity")
        desired = task.get("DesiredState")
        status = _object(task.get("Status"))
        state = status.get("State")
        if (
            not isinstance(desired, str)
            or desired not in _TASK_STATES
            or not isinstance(state, str)
            or state not in _TASK_STATES
        ):
            raise DokployEvidenceProviderError("container-config")
        if state != "running":
            pending += desired == "running"
            continue
        node_id = _identifier(task.get("NodeID"))
        container_id = _object(status.get("ContainerStatus")).get("ContainerID")
        if (
            not isinstance(container_id, str)
            or not _CONTAINER_ID.fullmatch(container_id)
            or container_id in containers
        ):
            raise DokployEvidenceProviderError("container-identity")
        containers.add(container_id)
        container = inspect(container_id)
        config = _object(container.get("Config"))
        labels = _object(config.get("Labels"))
        expected_labels = {
            "com.docker.swarm.service.id": service_id,
            "com.docker.swarm.service.name": app_name,
            "com.docker.swarm.task.id": task_id,
            "com.docker.swarm.node.id": node_id,
        }
        if container.get("Id") != container_id or any(
            labels.get(key) != value for key, value in expected_labels.items()
        ):
            raise DokployEvidenceProviderError("container-identity")
        container_state = _object(container.get("State"))
        if type(container_state.get("Running")) is not bool:
            raise DokployEvidenceProviderError("container-config")
        if (
            not container_state["Running"]
            or container_state.get("Paused")
            or container_state.get("Restarting")
        ):
            pending += 1
            continue
        task_image = _image(_object(_object(task.get("Spec")).get("ContainerSpec")).get("Image"))
        configured_image = _image(config.get("Image"))
        if _repository(task_image) != _repository(configured_image):
            raise DokployEvidenceProviderError("image-identity")
        image_id = container.get("Image")
        if not isinstance(image_id, str) or not _IMAGE_ID.fullmatch(image_id):
            raise DokployEvidenceProviderError("image-identity")
        image = inspect(image_id.removeprefix("sha256:"))
        digests = image.get("RepoDigests")
        if image.get("Id") != image_id or not isinstance(digests, list):
            raise DokployEvidenceProviderError("image-identity")
        matching = {
            value
            for value in digests
            if isinstance(value, str)
            and _repository(value) == _repository(configured_image)
            and docker_image_digest(value)
        }
        if len(matching) != 1:
            raise DokployEvidenceProviderError("image-identity")
        immutable_image = matching.pop()
        for reference in (task_image, configured_image):
            if "@" in reference and docker_image_digest(reference) != docker_image_digest(
                immutable_image
            ):
                raise DokployEvidenceProviderError("image-identity")
        _, record_id = _environment(config.get("Env"))
        health_status = (
            _object(container_state["Health"]).get("Status") if "Health" in container_state else ""
        )
        if not isinstance(health_status, str) or health_status not in {
            "",
            "starting",
            "healthy",
            "unhealthy",
        }:
            raise DokployEvidenceProviderError("container-config")
        images.append(immutable_image)
        record_ids.append(record_id)
        health.append(str(health_status))
    if inspect(app_name) != service:
        raise DokployEvidenceProviderError("target-inspect")
    current_target = api.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type="application",
        target_id=application_id,
    )
    if any(
        current_target.get(key) != target.get(key)
        for key in ("applicationId", "appName", "serverId")
    ):
        raise DokployEvidenceProviderError("target-inspect")
    return ApplicationRuntimeObservation(
        application_id=application_id,
        app_name=app_name,
        server_id=server_id,
        service_id=service_id,
        service_version=version,
        service_image=service_image,
        service_deployment_record_id=deployment_id,
        service_environment_keys=environment_keys,
        desired_replicas=replicas,
        rollout_state=rollout_state,
        rollout_started_at=started_at,
        rollout_completed_at=completed_at,
        running_container_images=tuple(images),
        running_container_deployment_record_ids=tuple(record_ids),
        running_container_health=tuple(health),
        pending_task_count=pending,
    )
