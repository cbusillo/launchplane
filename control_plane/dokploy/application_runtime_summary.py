"""Bounded, names-only application diagnostics, independent of release admission."""

from datetime import datetime

from control_plane.dokploy.application_runtime import (
    MAX_APPLICATION_DIAGNOSTIC_ITEMS,
    ApplicationEnvironmentKeys,
    ApplicationRuntimeObservation,
    application_immutable_image_matches,
)


def _environment_summary(keys: ApplicationEnvironmentKeys) -> dict[str, object]:
    return {"names": list(keys.names), "complete": keys.complete, "values_redacted": True}


def summarize_application_runtime(
    observed: ApplicationRuntimeObservation, *, expected_image: str, observed_at: datetime
) -> dict[str, object]:
    limit = MAX_APPLICATION_DIAGNOSTIC_ITEMS
    container_count = len(observed.running_container_images)
    complete = (
        observed.task_evidence_complete
        and container_count <= limit
        and len(observed.tasks) <= limit
        and container_count == len(observed.running_containers)
        and container_count == len(observed.running_container_deployment_record_ids)
        and container_count == len(observed.running_container_health)
        and all(0 < len(images) <= limit for images in observed.running_container_images)
    )
    image_matches: bool | None = None
    if complete and container_count:
        image_matches = all(
            any(application_immutable_image_matches(image, expected_image) for image in images)
            for images in observed.running_container_images
        )
    containers: list[dict[str, object]] = []
    for index, metadata in enumerate(observed.running_containers[:limit]):
        # Never silently zip away missing parallel facts from a partial read.
        containers.append(
            {
                "container_id": metadata.container_id,
                "task_id": metadata.task_id,
                "immutable_images": list(observed.running_container_images[index][:limit])
                if index < container_count
                else [],
                "deployment_record_id": observed.running_container_deployment_record_ids[index]
                if index < len(observed.running_container_deployment_record_ids)
                else "",
                "health": observed.running_container_health[index]
                if index < len(observed.running_container_health)
                else "",
                "environment_keys": _environment_summary(metadata.environment_keys),
            }
        )
    return {
        "kind": "application",
        "source": "provider_runtime_inspection",
        "observed_at": observed_at.isoformat(),
        "application_id": observed.application_id,
        "app_name": observed.app_name,
        "server_id": observed.server_id,
        "service_id": observed.service_id,
        "service_image": observed.service_image,
        "service_deployment_record_id": observed.service_deployment_record_id,
        "service_environment_keys": _environment_summary(observed.service_environment_keys),
        "desired_replicas": observed.desired_replicas,
        "rollout_state": observed.rollout_state,
        "rollout_started_at": observed.rollout_started_at,
        "initial_service": observed.initial_service,
        "pending_task_count": observed.pending_task_count,
        "task_evidence_complete": observed.task_evidence_complete,
        "diagnostic_complete": complete,
        "process_ready": observed.ready if complete else None,
        "expected_image": expected_image,
        "image_matches_expected": image_matches,
        "running_container_count": container_count,
        "containers": containers,
        "task_count": len(observed.tasks),
        "tasks": [
            {
                "task_id": task.task_id,
                "desired_state": task.desired_state,
                "state": task.state,
                "image": task.image,
                "deployment_record_id": task.deployment_record_id,
            }
            for task in observed.tasks[:limit]
        ],
        "details_truncated": (
            container_count > limit
            or len(observed.tasks) > limit
            or any(len(images) > limit for images in observed.running_container_images)
        ),
    }
