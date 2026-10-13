"""Service-owned prod routing from product configuration, without caller target grants."""

from dataclasses import dataclass
from collections.abc import Callable
from pathlib import Path
from typing import cast
import hashlib
import json

import click

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, PublicHostEffect
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
    effects: tuple[PublicHostEffect, ...]
    observed: tuple[api.JsonObject, ...] = ()

    def verify_provider(self) -> None:
        if (
            fetch_dokploy_compose_domains_for_target_setup(
                host=self.host, token=self.token, compose_id=self.target_id.target_id
            )
            != self.observed
        ):
            raise PublicHostsProviderError("Provider routes changed before configuration commit.")

    def apply_provider(self, checkpoint: Callable[[str, str], None]) -> None:
        def read() -> tuple[api.JsonObject, ...]:
            return fetch_dokploy_compose_domains_for_target_setup(
                host=self.host, token=self.token, compose_id=self.target_id.target_id
            )

        try:
            if read() != self.routes:
                raise PublicHostsProviderError("Provider routes changed; review a fresh dry-run.")
            for host in (*self.result.added, *self.result.updated):
                domain_id = compose.ensure_compose_web_domain_route(
                    host=self.host,
                    token=self.token,
                    compose_id=self.target_id.target_id,
                    domain_host=host,
                    runtime_port=self.result.runtime_port,
                )
                checkpoint(host, domain_id)
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
            managed = set(self.result.before) | set(self.result.after) | set(self.result.removed)
            if tuple(route for route in observed if route.get("host") not in managed) != tuple(
                route for route in self.routes if route.get("host") not in managed
            ):
                raise PublicHostsProviderError("Unmanaged provider routes changed during apply.")
            self.observed = observed
        except Exception as error:
            raise PublicHostsProviderError(
                "Public-host provider outcome needs inspection; no successful config receipt "
                "was committed. Pending effects remain tracked. Review a fresh dry-run; "
                "effects without a saved provider identity require inspection."
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
                (
                    *other.domains,
                    *other.public_hosts,
                    *(effect.host for effect in other.pending_public_host_effects),
                )
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
        effects = {effect.host: effect for effect in target.pending_public_host_effects}
        for effect in effects.values():
            if effect.target_id != target_id.target_id or effect.provider_host != host:
                raise ProductConfigError(
                    "Pending public-host effects belong to a different provider binding; inspect the target before retrying.",
                    code="public_hosts_effect_ownership_unknown",
                )
            selected = [route for route in routes if route.get("host") == effect.host]
            if selected and (
                len(selected) != 1
                or not effect.domain_id
                or selected[0].get("domainId") != effect.domain_id
                or not _route_matches(selected[0], effect.target_id, effect.runtime_port)
            ):
                raise ProductConfigError(
                    "Pending public-host effect has uncertain or changed ownership; inspect the target before retrying.",
                    code="public_hosts_effect_ownership_unknown",
                )
        before, after = set(target.public_hosts) | set(effects), set(hosts)
        added, updated, unchanged = [], [], []
        for public_host in sorted(before | after):
            selected = [route for route in routes if route.get("host") == public_host]
            if selected and public_host not in before:
                raise ValueError("Cannot adopt a pre-existing unmanaged public-host route.")
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
                if public_host not in target.public_hosts:
                    effects[public_host] = PublicHostEffect(
                        host=public_host,
                        provider_host=host,
                        target_id=target_id.target_id,
                        runtime_port=port,
                    )
            elif _route_matches(selected[0], target_id.target_id, port):
                unchanged.append(public_host)
            else:
                updated.append(public_host)
        removed = sorted(before - after)
        replacement = (
            target
            if hosts == target.public_hosts and not target.pending_public_host_effects
            else target.model_copy(
                update={
                    "public_hosts": hosts,
                    "pending_public_host_effects": (),
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
            target,
            target_id,
            provider_target,
            replacement,
            result,
            host,
            token,
            routes,
            tuple(effects.values()),
        )
    except ProductConfigError:
        raise
    except (FileNotFoundError, ValueError, click.ClickException) as error:
        raise ProductConfigError(
            "Public-host reconciliation needs an owned prod compose target and an "
            "unambiguous HTTPS web origin route; inspect the target before retrying.",
            code="public_hosts_target_not_ready",
        ) from error
