"""Service-owned prod routing from product configuration, without caller target grants."""

from dataclasses import dataclass
from pathlib import Path
from typing import cast
import hashlib
import json

import click

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.dokploy import api, compose, source
from control_plane.dokploy_target_setup_http import (
    delete_dokploy_domain_for_target_setup,
    fetch_dokploy_compose_domains_for_target_setup,
)
from control_plane.product_config import ProductConfigError
from control_plane.product_config_http import ProductConfigPublicHostsResult
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp


class PublicHostsProviderError(RuntimeError):
    """An effect may have happened; no successful config receipt was committed."""


def _route_matches(route: api.JsonObject, compose_id: str, port: int) -> bool:
    return all(
        route.get(key) == value
        for key, value in {
            "composeId": compose_id,
            "serviceName": "web",
            "port": port,
            "path": "/",
            "internalPath": "/",
            "https": True,
            "certificateType": "none",
            "stripPath": False,
        }.items()
    ) and not any(
        route.get(key) for key in ("applicationId", "previewDeploymentId", "customCertResolver")
    )


@dataclass
class PublicHostsPlan:
    target: DokployTargetRecord
    target_id: DokployTargetIdRecord
    provider_target: ProviderTargetRecord
    replacement: DokployTargetRecord
    result: ProductConfigPublicHostsResult
    host: str
    token: str
    routes: tuple[api.JsonObject, ...]

    def apply_provider(self) -> None:
        def read() -> tuple[api.JsonObject, ...]:
            return fetch_dokploy_compose_domains_for_target_setup(
                host=self.host, token=self.token, compose_id=self.target_id.target_id
            )

        try:
            if read() != self.routes:
                raise PublicHostsProviderError("Provider routes changed; review a fresh dry-run.")
            for host in (*self.result.added, *self.result.updated):
                compose.ensure_compose_web_domain_route(
                    host=self.host,
                    token=self.token,
                    compose_id=self.target_id.target_id,
                    domain_host=host,
                    runtime_port=self.result.runtime_port,
                )
            for route in self.routes:
                if route.get("host") in self.result.removed:
                    delete_dokploy_domain_for_target_setup(
                        host=self.host, token=self.token, domain_id=str(route["domainId"])
                    )
            observed = read()
            for host in self.result.after:
                selected = [route for route in observed if route.get("host") == host]
                if len(selected) != 1 or not _route_matches(
                    selected[0], self.target_id.target_id, self.result.runtime_port
                ):
                    raise PublicHostsProviderError("Public-host provider read-back did not match.")
            if any(route.get("host") in self.result.removed for route in observed):
                raise PublicHostsProviderError("Removed public hosts remain on the provider.")
            managed = set(self.result.before) | set(self.result.after)
            if tuple(route for route in observed if route.get("host") not in managed) != tuple(
                route for route in self.routes if route.get("host") not in managed
            ):
                raise PublicHostsProviderError("Unmanaged provider routes changed during apply.")
        except Exception as error:
            raise PublicHostsProviderError(
                "Public-host provider outcome needs inspection; no successful config receipt "
                "was committed. Routes may already exist. Rerun dry-run with the same host "
                "list and complete reconciliation before dropping names."
            ) from error


def plan_public_hosts(
    *,
    record_store: PostgresRecordStore,
    control_plane_root: Path,
    context: str,
    instance: str,
    hosts: tuple[str, ...],
    mode: str,
) -> PublicHostsPlan:
    try:
        target = record_store.read_dokploy_target_record(
            context_name=context, instance_name=instance
        )
        target_id = record_store.read_dokploy_target_id_record(
            context_name=context, instance_name=instance
        )
        provider_target = record_store.read_provider_target_record(
            context_name=context, instance_name=instance
        )
        if (
            instance != "prod"
            or target.target_type != "compose"
            or (
                provider_target.provider_id != "dokploy"
                or provider_target.target_id != target_id.target_id
                or provider_target.target_category != "compose"
            )
        ):
            raise ValueError("Public hosts require the owned prod compose binding.")
        protected = set(target.domains)
        if protected & set(hosts):
            raise ValueError("Public hosts cannot take ownership of an existing internal domain.")
        for other in record_store.list_dokploy_target_records():
            if (other.context, other.instance) != (context, instance) and set(hosts).intersection(
                (*other.domains, *other.public_hosts)
            ):
                raise ValueError("A public host is already owned by another target.")
        host, token = source.read_dokploy_config(control_plane_root=control_plane_root)
        routes = fetch_dokploy_compose_domains_for_target_setup(
            host=host, token=token, compose_id=target_id.target_id
        )
        # The existing origin route owns the service/port choice. Never invent a
        # default or take a port from a public route that is about to be repaired.
        origins = [
            route
            for route in routes
            if route.get("host") in protected and route.get("serviceName") == "web"
        ]
        ports = {route.get("port") for route in origins}
        if len(ports) != 1:
            raise ValueError("Public hosts need one unambiguous existing web port.")
        port = next(iter(ports))
        if (
            type(port) is not int
            or not 1 <= port <= 65535
            or not all(_route_matches(route, target_id.target_id, port) for route in origins)
        ):
            raise ValueError("Public hosts require an existing HTTPS web origin route.")
        before, after = set(target.public_hosts), set(hosts)
        added, updated, unchanged = [], [], []
        for public_host in sorted(before | after):
            selected = [route for route in routes if route.get("host") == public_host]
            if len(selected) > 1 or any(not route.get("domainId") for route in selected):
                raise ValueError("Public-host routes must have unique provider identities.")
            if selected and (
                selected[0].get("composeId") != target_id.target_id
                or selected[0].get("serviceName") != "web"
                or selected[0].get("path") != "/"
            ):
                raise ValueError("Public-host route conflicts with another service or path.")
            if public_host not in after:
                if selected and (
                    type(selected[0].get("port")) is not int
                    or not _route_matches(
                        selected[0], target_id.target_id, cast(int, selected[0]["port"])
                    )
                ):
                    raise ValueError(
                        "Refusing to remove a public-host route with changed ownership."
                    )
            elif not selected:
                added.append(public_host)
            elif _route_matches(selected[0], target_id.target_id, port):
                unchanged.append(public_host)
            else:
                updated.append(public_host)
        removed = sorted(before - after)
        replacement = (
            target
            if hosts == target.public_hosts
            else target.model_copy(
                update={
                    "public_hosts": hosts,
                    "updated_at": utc_now_timestamp(),
                    "source_label": "service:product-config:public-hosts",
                }
            )
        )
        result = ProductConfigPublicHostsResult(
            plan_digest=hashlib.sha256(
                json.dumps(
                    {
                        "target": target.model_dump(mode="json"),
                        "target_id": target_id.model_dump(mode="json"),
                        "provider_target": provider_target.model_dump(mode="json"),
                        "routes": routes,
                        "hosts": hosts,
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest(),
            before=list(target.public_hosts),
            after=list(hosts),
            added=added,
            updated=updated,
            removed=removed,
            unchanged=unchanged,
            runtime_port=port,
            verified=mode == "apply",
            read_back_hosts=list(hosts) if mode == "apply" else [],
        )
        return PublicHostsPlan(
            target, target_id, provider_target, replacement, result, host, token, routes
        )
    except (FileNotFoundError, ValueError, click.ClickException) as error:
        raise ProductConfigError(
            "Public-host reconciliation needs an owned prod compose target and an "
            "unambiguous HTTPS web origin route; inspect the target before retrying.",
            code="public_hosts_target_not_ready",
        ) from error
