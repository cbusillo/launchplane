"""Value-free provider inventory, independent of Launchplane's tracked products."""

from __future__ import annotations

import hashlib
import http.client
import json
import time
from collections.abc import Callable, Mapping
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict

from control_plane.dokploy import api

# project.all is unpaginated. Bound the upstream snapshot and each detail page.
MAX_PROJECTS = 2000
MAX_ENVIRONMENTS = 10000
MAX_TARGETS = 20000
MAX_PAGE_TARGETS = 500
TARGET_COLLECTIONS = {
    "applications": ("application", "applicationId"),
    "compose": ("compose", "composeId"),
    "postgres": ("postgres", "postgresId"),
    "mysql": ("mysql", "mysqlId"),
    "mariadb": ("mariadb", "mariadbId"),
    "mongo": ("mongo", "mongoId"),
    "redis": ("redis", "redisId"),
    "libsql": ("libsql", "libsqlId"),
}


class InventoryUnavailable(Exception):
    """Only a fixed public code leaves this boundary; never provider error text."""


class InventoryChanged(Exception):
    pass


class InventoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Placement(InventoryModel):
    kind: Literal["server", "dokploy_host"]
    server_id: str | None
    server_name: str | None


class InventoryTarget(InventoryModel):
    target_id: str
    category: str
    name: str
    placement: Placement


class InventoryEnvironment(InventoryModel):
    environment_id: str
    name: str
    targets: list[InventoryTarget]


class InventoryProject(InventoryModel):
    project_id: str
    name: str
    environments: list[InventoryEnvironment]


class InventoryPage(InventoryModel):
    scope: Literal["provider_credential_visible"] = "provider_credential_visible"
    provider_pagination: Literal["unpaginated_project_all"] = "unpaginated_project_all"
    inventory_digest: str
    total_projects: int
    total_environments: int
    total_targets: int
    offset: int
    limit: int
    next_offset: int | None
    projects: list[InventoryProject]


class InventoryResponse(InventoryModel):
    status: Literal["ok"] = "ok"
    trace_id: str
    inventory: InventoryPage


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 256:
        raise InventoryUnavailable("invalid_metadata")
    if any(ord(character) < 32 for character in value):
        raise InventoryUnavailable("invalid_metadata")
    return value


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise InventoryUnavailable("invalid_shape")
    return value


def _items(value: object) -> list[object]:
    if not isinstance(value, list):
        raise InventoryUnavailable("incomplete_collections")
    return value


def _read(
    *,
    host: str,
    token: str,
    path: str,
    deadline: float,
    clock: Callable[[], float],
    query: dict[str, str | int] | None = None,
) -> object:
    remaining = deadline - clock()
    if remaining <= 0:
        raise InventoryUnavailable("read_budget")
    try:
        return api.dokploy_request(
            host=host,
            token=token,
            path=path,
            query=query,
            method="GET",
            timeout_seconds=min(10, remaining),
        )
    except (click.ClickException, OSError, ValueError, http.client.HTTPException) as error:
        raise InventoryUnavailable("provider_read_failed") from error


def read_dokploy_inventory(
    *,
    host: str,
    token: str,
    offset: int,
    limit: int,
    inventory_digest: str = "",
    clock: Callable[[], float] = time.monotonic,
) -> InventoryPage:
    deadline = clock() + 25
    raw_projects = _items(
        _read(host=host, token=token, path="/api/project.all", deadline=deadline, clock=clock)
    )
    if len(raw_projects) > MAX_PROJECTS:
        raise InventoryUnavailable("snapshot_limit")
    projects: list[InventoryProject] = []
    project_ids: set[str] = set()
    environment_ids: set[str] = set()
    target_ids: set[tuple[str, str]] = set()
    for raw_project in raw_projects:
        project_data = _object(raw_project)
        project_id = _text(project_data.get("projectId"))
        if project_id in project_ids:
            raise InventoryUnavailable("duplicate_identity")
        project_ids.add(project_id)
        environments: list[InventoryEnvironment] = []
        for raw_environment in _items(project_data.get("environments")):
            environment_data = _object(raw_environment)
            environment_id = _text(environment_data.get("environmentId"))
            if environment_id in environment_ids:
                raise InventoryUnavailable("duplicate_identity")
            environment_ids.add(environment_id)
            targets: list[InventoryTarget] = []
            for collection, (category, id_field) in TARGET_COLLECTIONS.items():
                for raw_target in _items(environment_data.get(collection)):
                    target_data = _object(raw_target)
                    target_id = _text(target_data.get(id_field))
                    identity = (category, target_id)
                    if identity in target_ids:
                        raise InventoryUnavailable("duplicate_identity")
                    target_ids.add(identity)
                    # Names/placement are hydrated only for this page: project.all
                    # omits them for databases, and omits placement for all categories.
                    targets.append(
                        InventoryTarget(
                            target_id=target_id,
                            category=category,
                            name="pending",
                            placement=Placement(
                                kind="dokploy_host", server_id=None, server_name=None
                            ),
                        )
                    )
            environments.append(
                InventoryEnvironment(
                    environment_id=environment_id,
                    name=_text(environment_data.get("name")),
                    targets=sorted(targets, key=lambda target: (target.category, target.target_id)),
                )
            )
        projects.append(
            InventoryProject(
                project_id=project_id,
                name=_text(project_data.get("name")),
                environments=sorted(
                    environments, key=lambda environment: environment.environment_id
                ),
            )
        )
        if len(environment_ids) > MAX_ENVIRONMENTS or len(target_ids) > MAX_TARGETS:
            raise InventoryUnavailable("snapshot_limit")
    projects.sort(key=lambda project: project.project_id)
    digest = hashlib.sha256(
        json.dumps(
            [project.model_dump() for project in projects], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    if inventory_digest and inventory_digest != digest:
        raise InventoryChanged
    if offset and not inventory_digest:
        raise ValueError("Continuation requires inventory_digest from the first page.")
    if offset > len(projects):
        raise ValueError("Offset exceeds the visible project inventory.")
    selected = projects[offset : offset + limit]
    if (
        sum(
            len(environment.targets) for project in selected for environment in project.environments
        )
        > MAX_PAGE_TARGETS
    ):
        raise InventoryUnavailable("page_target_limit")
    servers: dict[str, str] = {}
    for project in selected:
        for environment in project.environments:
            for target in environment.targets:
                id_field = TARGET_COLLECTIONS[
                    "applications" if target.category == "application" else target.category
                ][1]
                detail = _object(
                    _read(
                        host=host,
                        token=token,
                        deadline=deadline,
                        clock=clock,
                        path=f"/api/{target.category}.one",
                        query={id_field: target.target_id},
                    )
                )
                if (
                    detail.get(id_field) != target.target_id
                    or detail.get("environmentId") != environment.environment_id
                ):
                    raise InventoryUnavailable("target_identity_changed")
                target.name = _text(detail.get("name"))
                # Missing is unknown; an explicit null is Dokploy's local host.
                if "serverId" not in detail:
                    raise InventoryUnavailable("missing_placement")
                if detail["serverId"] is None:
                    continue
                server_id = _text(detail["serverId"])
                if server_id not in servers:
                    server = _object(
                        _read(
                            host=host,
                            token=token,
                            deadline=deadline,
                            clock=clock,
                            path="/api/server.one",
                            query={"serverId": server_id},
                        )
                    )
                    if server.get("serverId") != server_id:
                        raise InventoryUnavailable("server_identity_changed")
                    servers[server_id] = _text(server.get("name"))
                target.placement = Placement(
                    kind="server", server_id=server_id, server_name=servers[server_id]
                )
    next_offset = offset + len(selected)
    return InventoryPage(
        inventory_digest=digest,
        total_projects=len(projects),
        total_environments=len(environment_ids),
        total_targets=len(target_ids),
        offset=offset,
        limit=limit,
        next_offset=next_offset if next_offset < len(projects) else None,
        projects=selected,
    )
