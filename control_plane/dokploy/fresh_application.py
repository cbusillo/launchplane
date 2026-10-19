"""The vanilla runtime shape of a newly allocated, never-deployed application."""

from control_plane.dokploy.api import JsonObject
from control_plane.dokploy.target_source_setup import require_empty_target_source


def _require_empty_environment(payload: JsonObject) -> None:
    value = payload.get("env")
    if "env" not in payload or (
        value is not None and (not isinstance(value, str) or value.strip())
    ):
        raise ValueError("Fresh application environment inputs changed.")


def require_fresh_application_runtime(
    payload: JsonObject, *, environment_id: str, project_id: str
) -> None:
    """Accept provider defaults, not custom service execution or resource bindings.

    Dokploy's application.one includes environment.project and the related runtime
    collections. Its Docker-source deploy skips source builds, but retains these
    service inputs. Nullable Swarm objects must remain null: even an empty object
    overrides provider defaults such as the volume's manager placement constraint.
    """
    require_empty_target_source(payload)
    _require_empty_environment(payload)
    environment = payload.get("environment")
    if not isinstance(environment, dict) or (
        environment.get("environmentId") != environment_id
        or environment.get("projectId") != project_id
    ):
        raise ValueError("Fresh application environment identity changed.")
    project = environment.get("project")
    if not isinstance(project, dict) or project.get("projectId") != project_id:
        raise ValueError("Fresh application project identity changed.")
    _require_empty_environment(environment)
    _require_empty_environment(project)

    # These bindings can authenticate, upload an image or execute even for a Docker
    # source. Source-build paths, stages, arguments and secrets do not affect it.
    for key in (
        "username",
        "password",
        "registryUrl",
        "registryId",
        "buildRegistryId",
        "rollbackRegistryId",
        "buildServerId",
        "command",
        "memoryReservation",
        "memoryLimit",
        "cpuReservation",
        "cpuLimit",
    ):
        if payload.get(key) not in (None, ""):
            raise ValueError("Fresh application has a custom runtime binding.")
    for key in ("registry", "buildRegistry", "rollbackRegistry"):
        if payload.get(key) is not None:
            raise ValueError("Fresh application has a registry binding.")
    if any(value is not None for key, value in payload.items() if key.endswith("Swarm")):
        raise ValueError("Fresh application has a custom Swarm service configuration.")
    for key in ("args", "networkIds"):
        if payload.get(key) not in (None, []):
            raise ValueError("Fresh application has custom arguments or networks.")
    if payload.get("detachDokployNetwork", False) is not False:
        raise ValueError("Fresh application has custom network attachment.")
    if type(payload.get("replicas")) is not int or payload["replicas"] != 1:
        raise ValueError("Fresh application must retain its single default replica.")
    # application.one always hydrates these relations. Missing evidence is not an
    # empty collection. The caller separately rechecks the supported domain shape.
    for key in ("ports", "security", "redirects", "deployments", "previewDeployments"):
        if payload.get(key) != []:
            raise ValueError("Fresh application already has runtime or ingress configuration.")
    if payload.get("applicationStatus") != "idle":
        raise ValueError("Fresh application already has execution status.")
