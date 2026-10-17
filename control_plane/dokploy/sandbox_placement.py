"""Complete uniquely named disposable placement inventory; no lane adoption."""

from pathlib import Path
from control_plane.dokploy import api, source


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
