from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import click

from control_plane import dokploy as control_plane_dokploy
from control_plane.dokploy import JsonObject


PreviewResourceType = Literal["application", "compose"]
PreviewResourceDestroyStatus = Literal["pass", "fail"]


@dataclass(frozen=True)
class PreviewResourceDestroyStep:
    name: str
    target: str


@dataclass(frozen=True)
class PreviewResourceDestroyResult:
    status: PreviewResourceDestroyStatus
    domain_ids: tuple[str, ...] = ()
    steps: tuple[PreviewResourceDestroyStep, ...] = ()
    cleanup_errors: tuple[str, ...] = ()


def destroy_dokploy_preview_resource(
    *,
    host: str,
    token: str,
    resource_type: PreviewResourceType,
    resource_id: str,
    domain_host: str = "",
    delete_volumes: bool = False,
    continue_after_domain_cleanup_error: bool = True,
    missing_resource_is_clean: bool = False,
    before_provider_mutation: Callable[[str], None] | None = None,
) -> PreviewResourceDestroyResult:
    normalized_resource_id = resource_id.strip()
    normalized_domain_host = domain_host.strip().lower()
    steps: list[PreviewResourceDestroyStep] = []
    cleanup_errors: list[str] = []
    domain_ids: list[str] = []

    try:
        domain_ids = list(
            _preview_resource_domain_ids(
                host=host,
                token=token,
                resource_type=resource_type,
                resource_id=normalized_resource_id,
                domain_host=normalized_domain_host,
            )
        )
        steps.append(PreviewResourceDestroyStep("domain_lookup", normalized_resource_id))
        for domain_id in domain_ids:
            if before_provider_mutation is not None:
                before_provider_mutation("domain_delete")
            _delete_domain(host=host, token=token, domain_id=domain_id)
            steps.append(PreviewResourceDestroyStep("domain_delete", domain_id))
    except click.ClickException as exc:
        cleanup_errors.append(f"domain cleanup failed: {exc}")
        if not continue_after_domain_cleanup_error:
            return PreviewResourceDestroyResult(
                status="fail",
                domain_ids=tuple(domain_ids),
                steps=tuple(steps),
                cleanup_errors=tuple(cleanup_errors),
            )

    try:
        if before_provider_mutation is not None:
            before_provider_mutation(f"{resource_type}_destroy")
        _delete_preview_resource(
            host=host,
            token=token,
            resource_type=resource_type,
            resource_id=normalized_resource_id,
            delete_volumes=delete_volumes,
        )
        steps.append(PreviewResourceDestroyStep(f"{resource_type}_delete", normalized_resource_id))
    except click.ClickException as exc:
        if not (missing_resource_is_clean and _is_resource_delete_not_found(exc)):
            cleanup_errors.append(f"{resource_type} cleanup failed: {exc}")
        else:
            steps.append(
                PreviewResourceDestroyStep(f"{resource_type}_delete", normalized_resource_id)
            )

    return PreviewResourceDestroyResult(
        status="fail" if cleanup_errors else "pass",
        domain_ids=tuple(domain_ids),
        steps=tuple(steps),
        cleanup_errors=tuple(cleanup_errors),
    )


def _preview_resource_domain_ids(
    *,
    host: str,
    token: str,
    resource_type: PreviewResourceType,
    resource_id: str,
    domain_host: str,
) -> tuple[str, ...]:
    raw_domains = control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path=_domain_lookup_path(resource_type),
        query={_resource_id_key(resource_type): resource_id},
    )
    if not isinstance(raw_domains, list):
        return ()
    domain_ids: list[str] = []
    for raw_domain in raw_domains:
        domain = control_plane_dokploy.as_json_object(raw_domain)
        if domain is None:
            continue
        candidate_domain_id = str(domain.get("domainId") or "").strip()
        candidate_domain_host = str(domain.get("host") or "").strip().lower()
        if candidate_domain_id and (not domain_host or candidate_domain_host == domain_host):
            domain_ids.append(candidate_domain_id)
    return tuple(domain_ids)


def _delete_domain(*, host: str, token: str, domain_id: str) -> None:
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path="/api/domain.delete",
        method="POST",
        payload={"domainId": domain_id},
    )


def _delete_preview_resource(
    *,
    host: str,
    token: str,
    resource_type: PreviewResourceType,
    resource_id: str,
    delete_volumes: bool,
) -> None:
    payload: JsonObject = {_resource_id_key(resource_type): resource_id}
    if resource_type == "compose":
        payload["deleteVolumes"] = delete_volumes
    control_plane_dokploy.dokploy_request(
        host=host,
        token=token,
        path=_delete_path(resource_type),
        method="POST",
        payload=payload,
    )


def _domain_lookup_path(resource_type: PreviewResourceType) -> str:
    if resource_type == "application":
        return "/api/domain.byApplicationId"
    return "/api/domain.byComposeId"


def _delete_path(resource_type: PreviewResourceType) -> str:
    if resource_type == "application":
        return "/api/application.delete"
    return "/api/compose.delete"


def _resource_id_key(resource_type: PreviewResourceType) -> str:
    if resource_type == "application":
        return "applicationId"
    return "composeId"


def _is_resource_delete_not_found(exc: click.ClickException) -> bool:
    message = str(exc).lower()
    return "404" in message and "not found" in message
