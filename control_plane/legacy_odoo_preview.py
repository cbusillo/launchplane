"""Read-only, complete compose absence proof for an owned legacy Odoo preview."""

from collections.abc import Mapping
from pathlib import Path
import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import yaml

from control_plane.contracts.product_retirement import canonical_sha256
from control_plane.contracts.deploy_reference import docker_image_repository
from control_plane.dokploy import api, source
from control_plane.dokploy.preview_inventory import complete_preview_compose_inventory

if TYPE_CHECKING:
    from control_plane.legacy_preview_reconciliation import LegacyPreviewAuthority


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError("Compose identity or source evidence is incomplete.")
    return value.strip()


def _images(payload: Mapping[str, object]) -> tuple[str, ...]:
    """Inspect image identities only. Never return compose files or environment values."""
    raw = payload.get("composeFile")
    if not isinstance(raw, str) or not raw.strip() or len(raw.encode()) > 2_000_000:
        raise ValueError("Unowned compose source is unavailable.")
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise ValueError("Unowned compose source is malformed.") from error
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict) or not services:
        raise ValueError("Unowned compose services are unavailable.")
    images: list[str] = []
    for service in services.values():
        if not isinstance(service, dict) or "build" in service:
            raise ValueError("Unowned compose build source is uncertain.")
        image = _text(service.get("image"))
        variable = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", image)
        if variable:
            environment = payload.get("env")
            if not isinstance(environment, str):
                raise ValueError("Unowned compose image binding is unavailable.")
            key = variable.group(1)
            lines = [
                line.strip().removeprefix("export ").lstrip() for line in environment.splitlines()
            ]
            matches = [line for line in lines if line.partition("=")[0].strip() == key]
            if len(matches) != 1:
                raise ValueError("Unowned compose image binding is ambiguous.")
            image = _text(api.parse_dokploy_env_text(environment).get(key))
        if "$" in image:
            raise ValueError("Unowned compose image cannot be resolved safely.")
        images.append(image)
    return tuple(images)


def _repository_matches(payload: Mapping[str, object], repository: str) -> bool:
    values: list[str] = []
    for key in (
        "repository",
        "customGitUrl",
        "githubRepository",
        "gitlabRepository",
        "giteaRepository",
        "bitbucketRepository",
    ):
        value = payload.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError("Provider repository evidence is uncertain.")
        values.append(value.strip().casefold().rstrip("/").removesuffix(".git"))
    return any(
        value == repository
        or value.endswith("/" + repository)
        or value == repository.partition("/")[2]
        for value in values
    )


def observe_legacy_odoo_preview(
    *, control_plane_root: Path, bound: "LegacyPreviewAuthority"
) -> dict[str, object]:
    host, token = source.read_dokploy_config(control_plane_root=control_plane_root)
    inventory = complete_preview_compose_inventory(host=host, token=token)
    domain = urlsplit(bound.preview.canonical_url).hostname
    if not domain:
        raise ValueError("Preview has no usable domain evidence.")
    image_repositories = {bound.profile.image.repository} | {
        docker_image_repository(g.runtime_identity.image_reference)
        for g in bound.generations
        if g.runtime_identity and g.runtime_identity.image_reference
    }
    seen: dict[str, object] = {}
    present = False
    repository = bound.profile.repository.casefold()
    for environment, identifier, name in inventory:
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=identifier
        )
        if (
            payload.get("composeId") != identifier
            or payload.get("environmentId") != environment
            or _text(payload.get("name")) != name
        ):
            raise ValueError("Compose identity changed during inspection.")
        app_name = _text(payload.get("appName"))
        raw_domains = api.dokploy_request(
            host=host,
            token=token,
            path="/api/domain.byComposeId",
            query={"composeId": identifier},
        )
        if not isinstance(raw_domains, list):
            raise ValueError("Compose domain inventory is incomplete.")
        domains: list[str] = []
        for item in raw_domains:
            if not isinstance(item, dict) or item.get("composeId", identifier) != identifier:
                raise ValueError("Compose domain ownership is uncertain.")
            domains.append(_text(item.get("host") or item.get("domain")).casefold())
        exact = (
            identifier in bound.preview_target_ids
            or name.casefold() == bound.application_name.casefold()
            or app_name.casefold() == bound.application_name.casefold()
            or app_name.casefold().startswith(bound.application_name.casefold() + "-")
            or domain.casefold() in domains
        )
        if exact and identifier in bound.known_other_target_ids:
            raise ValueError("Preview candidate belongs to a stable lane or sibling.")
        known_other = identifier in bound.known_other_target_ids
        product_source = False
        if not known_other and not exact:
            images = _images(payload)
            product_source = any(
                image == repo or image.startswith((repo + ":", repo + "@"))
                for image in images
                for repo in image_repositories
            ) or _repository_matches(payload, repository)
            prefix = bound.application_name.removesuffix(bound.slug).casefold()
            if (
                product_source
                or name.casefold().startswith(prefix)
                or app_name.casefold().startswith(bound.profile.product.casefold() + "-")
            ):
                raise ValueError("Provider has an unbound or renamed product candidate.")
        present = present or exact
        seen[identifier] = {
            "environment": environment,
            "name": name,
            "app_name": app_name,
            "domains": sorted(domains),
            "product_source": product_source,
        }
    applications = api.search_dokploy_applications(host=host, token=token)
    application_ids: set[str] = set()
    for item in applications:
        identifier = _text(item.get("applicationId") or item.get("id"))
        if identifier in application_ids:
            raise ValueError("Application inventory identity is duplicated.")
        application_ids.add(identifier)
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="application", target_id=identifier
        )
        if payload.get("applicationId") != identifier:
            raise ValueError("Application identity changed during inspection.")
        name, app_name = _text(payload.get("name")), _text(payload.get("appName"))
        application_domains = api.fetch_dokploy_application_domains(
            host=host, token=token, application_id=identifier, strict=True
        )
        domains = []
        for entry in application_domains:
            if entry.get("applicationId", identifier) != identifier:
                raise ValueError("Application domain ownership is uncertain.")
            domains.append(_text(entry.get("host") or entry.get("domain")).casefold())
        exact = (
            domain.casefold() in domains
            or name.casefold() == bound.application_name.casefold()
            or app_name.casefold() == bound.application_name.casefold()
            or app_name.casefold().startswith(bound.application_name.casefold() + "-")
        )
        known_other = identifier in bound.known_other_application_ids
        if exact and known_other:
            raise ValueError("Preview application candidate belongs to another lane.")
        if not exact and not known_other:
            image = payload.get("dockerImage", "")
            if not isinstance(image, str):
                raise ValueError("Application image evidence is uncertain.")
            prefix = bound.application_name.removesuffix(bound.slug).casefold()
            if (
                name.casefold().startswith(prefix)
                or app_name.casefold().startswith(bound.profile.product.casefold() + "-")
                or _repository_matches(payload, repository)
                or any(
                    image == repo or image.startswith((repo + ":", repo + "@"))
                    for repo in image_repositories
                )
            ):
                raise ValueError("Provider has an unbound product application candidate.")
        present = present or exact
        seen["application:" + identifier] = {
            "name": name,
            "app_name": app_name,
            "domains": sorted(domains),
        }
    final_applications = api.search_dokploy_applications(host=host, token=token)
    if sorted(_text(a.get("applicationId") or a.get("id")) for a in final_applications) != sorted(
        application_ids
    ):
        raise ValueError("Application inventory changed during inspection.")
    if complete_preview_compose_inventory(host=host, token=token) != inventory:
        raise ValueError("Compose inventory changed during inspection.")
    return {
        "provider_state": "present" if present else "absent",
        "provider_absence_verified": not present,
        "inventory_digest": canonical_sha256(seen),
        "provider_writes": False,
    }
