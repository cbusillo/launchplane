"""Complete uniquely named disposable placement inventory; no lane adoption."""

from pathlib import Path
from collections.abc import Callable
import re

from control_plane.dokploy import api, source


class IsolatedPlacementObservationRequired(ValueError):
    """A dispatched create lacks its identity; observe the original operation."""


def create_isolated_placement(
    root: Path,
    *,
    namespace: str,
    server_id: str,
    description: str,
    checkpoint: Callable[[], None],
    before_create: Callable[[str], None],
    record_identity: Callable[[str, str], None],
    recorded_placement: tuple[str, str, str] = ("", "", ""),
) -> None:
    """Create an unrouted placement, without adopting a product's serving lane.

    The caller owns the durable operation and records each returned identity
    immediately. An interrupted create must go through original-key observation;
    this entry point only continues a recorded placement that matches complete
    provider inventory. The caller excludes uncertain dispatches before calling.
    No source, credentials, domain or deployment is installed here.
    """
    from control_plane.workflows.dokploy_target_adoption import (
        PROJECT_CREATE_PATH,
        ENVIRONMENT_CREATE_PATH,
        COMPOSE_CREATE_PATH,
    )

    if (
        not re.fullmatch(r"lp-(?:testing|rehearsal)-[a-f0-9]{32}", namespace)
        or not server_id.strip()
    ):
        raise ValueError("Isolated placement requires its service namespace and selected server.")
    checkpoint()
    if created_placement(root, namespace) != recorded_placement:
        raise ValueError("Existing isolated placement requires original-key observation.")
    host, token = source.read_dokploy_config(control_plane_root=root)
    fields = ("project_id", "environment_id", "compose_id")
    identities = dict(zip(fields, recorded_placement, strict=True))
    if any(recorded_placement[index] and not recorded_placement[index - 1] for index in (1, 2)):
        raise ValueError("Recorded isolated placement has missing parents.")
    creates: tuple[tuple[str, str, str, Callable[[], api.JsonObject]], ...] = (
        (
            "project_id",
            "projectId",
            PROJECT_CREATE_PATH,
            lambda: {"name": namespace, "description": description},
        ),
        (
            "environment_id",
            "environmentId",
            ENVIRONMENT_CREATE_PATH,
            lambda: {
                "name": namespace,
                "projectId": identities["project_id"],
                "description": description,
            },
        ),
        (
            "compose_id",
            "composeId",
            COMPOSE_CREATE_PATH,
            lambda: {
                "name": namespace,
                "appName": namespace,
                "environmentId": identities["environment_id"],
                "serverId": server_id,
                "composeType": "docker-compose",
            },
        ),
    )
    for field, response_key, path, payload in creates:
        if identities[field]:
            continue
        checkpoint()
        before_create(field)
        response = api.as_json_object(
            api.dokploy_request(host=host, token=token, path=path, method="POST", payload=payload())
        )
        identifier = (response or {}).get(response_key)
        if not isinstance(identifier, str) or not identifier.strip():
            raise IsolatedPlacementObservationRequired(
                "Isolated creation is uncertain; use original-key observation."
            )
        record_identity(field, identifier)
        identities[field] = identifier


def created_placement(root: Path, namespace: str) -> tuple[str, str, str]:
    host, token = source.read_dokploy_config(control_plane_root=root)
    matches = [
        project
        for project in api.fetch_dokploy_projects(host=host, token=token)
        if project.get("name") == namespace
    ]
    if not matches:
        return "", "", ""
    if len(matches) != 1 or not matches[0].get("projectId"):
        raise ValueError("Fresh project inventory is ambiguous.")
    project = matches[0]
    environments = project.get("environments")
    if not isinstance(environments, list):
        raise ValueError("Fresh project placement is not empty.")
    matching = []
    for candidate in environments:
        if not isinstance(candidate, dict):
            raise ValueError("Fresh environment inventory is malformed.")
        for key in ("applications", "postgres", "mysql", "mariadb", "mongo", "redis"):
            if candidate.get(key):
                raise ValueError("Fresh placement contains another resource.")
        if candidate.get("name") == namespace:
            matching.append(candidate)
        elif any(candidate.get(key) not in (None, []) for key in ("compose", "composes")):
            raise ValueError("Fresh project contains another compose.")
    if not matching:
        return str(project["projectId"]), "", ""
    if len(matching) != 1 or not matching[0].get("environmentId"):
        raise ValueError("Fresh environment inventory is ambiguous.")
    environment = matching[0]
    wrappers = [environment[key] for key in ("compose", "composes") if key in environment]
    if len(wrappers) != 1:
        raise ValueError("Fresh compose inventory is incomplete or ambiguous.")
    composes = wrappers[0]
    if not isinstance(composes, list) or len(composes) > 1:
        raise ValueError("Fresh compose inventory is incomplete or ambiguous.")
    compose_id = ""
    if composes:
        compose = composes[0]
        if (
            not isinstance(compose, dict)
            or compose.get("name") != namespace
            or not compose.get("composeId")
        ):
            raise ValueError("Fresh compose inventory has another identity.")
        compose_id = str(compose["composeId"])
    return str(project["projectId"]), str(environment["environmentId"]), compose_id
