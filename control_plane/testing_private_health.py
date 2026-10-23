"""Health-only transport through a freshly verified testing edge route."""

import os
from pathlib import Path
from typing import cast
from urllib.parse import urlsplit

import click
from sqlalchemy.exc import SQLAlchemyError

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.private_health_endpoint_record import (
    private_health_endpoint_record_sha256,
)
from control_plane.outbound_http import (
    PublicHttpDestinationError,
    PublicHttpResponse,
    _parse_http_url,
    request_private_edge_health,
    resolve_private_http_destination,
)
from control_plane.npmplus import NpmplusClient, NpmplusProxyHost
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_private_ingress import (
    TestingPrivateIngressAdapter,
    TestingPrivateIngressRequest,
    latest_testing_private_ingress_receipt,
)
from control_plane.workflows.ingress_provider import (
    NPMPLUS_BASE_URL_ENV_KEY,
    build_npmplus_ingress_client_from_env,
)


def _edge_health_origin(client: NpmplusClient, api_url: str) -> tuple[str, NpmplusProxyHost | None]:
    parsed, hostname, port = _parse_http_url(api_url)
    if parsed.query or parsed.fragment:
        raise ValueError("The edge API origin is ambiguous.")
    if port != (443 if parsed.scheme == "https" else 80):
        return api_url, None
    health_hostname = f"[{hostname}]" if ":" in hostname else hostname
    try:
        resolve_private_http_destination(f"https://{health_hostname}")
    except PublicHttpDestinationError as error:
        if error.code != "private_url":
            raise
    else:
        # Preserve an already-private API's existing listener identity. Its
        # self-proxy may target loopback or a provider-local container instead.
        return api_url, None
    hosts = tuple(host for host in client.list_proxy_hosts() if hostname in host.domain_names)
    if len(hosts) != 1:
        raise ValueError("No unique current edge API route.")
    host = client.get_proxy_host(hosts[0].id, public_only=True)
    if (
        host.id != hosts[0].id
        or host.domain_names != (hostname,)
        or not host.enabled
        or host.locations
        or host.advanced_config.strip()
        or host.npmplus_auth_request != "none"
        or host.forward_scheme not in {"http", "https"}
        or host.forward_port is None
        or (
            parsed.scheme == "https"
            and (not isinstance(host.certificate_id, int) or host.certificate_id < 1)
        )
        or hostname in client.list_other_host_domains()
    ):
        raise ValueError("The edge API needs an exact loaded ordinary proxy route.")
    origin = f"https://{host.forward_host}"
    forward, _hostname, _port = _parse_http_url(origin)
    if (
        forward.port is not None
        or forward.path
        or forward.query
        or forward.fragment
        or forward.netloc != host.forward_host
        or any(marker in host.forward_host for marker in ("*", "~", "\\"))
    ):
        raise ValueError("The edge API upstream must name one host.")
    return origin, host


def fetch_testing_private_health(
    *,
    root: Path,
    store: PostgresRecordStore,
    product: str,
    context: str,
    instance: str,
    endpoint_key: str,
    endpoint_sha256: str,
    url: str,
    timeout_seconds: int,
    route_activation: object = None,
) -> PublicHttpResponse:
    """A receipt is a claim to check, never permission to probe an arbitrary URL."""
    try:
        endpoint = store.read_private_health_endpoint_record(endpoint_key)
        if (
            instance != "testing"
            or (endpoint.product, endpoint.context, endpoint.instance, endpoint.status)
            != (product, context, instance, "active")
            or endpoint.url != url
            or private_health_endpoint_record_sha256(endpoint) != endpoint_sha256
        ):
            raise ValueError("Testing health endpoint changed.")
        api_url = os.environ.get(NPMPLUS_BASE_URL_ENV_KEY, "")
        client = build_npmplus_ingress_client_from_env()
        edge_origin = _edge_health_origin(client, api_url)
        hostname = urlsplit(url).hostname
        hosts = tuple(host for host in client.list_proxy_hosts() if hostname in host.domain_names)
        if len(hosts) != 1:
            raise ValueError("No unique current testing edge route.")
        host = client.get_proxy_host(hosts[0].id, public_only=True)
        receipt = latest_testing_private_ingress_receipt(
            store, product=product, context=context, host_id=host.id
        )
        if (
            receipt is None
            or receipt.tls_owner != "provider"
            or not host.enabled
            or host.access_list_id < 1
            or not isinstance(host.certificate_id, int)
            or host.certificate_id < 1
        ):
            raise ValueError("No current private testing edge receipt.")
        adapter = TestingPrivateIngressAdapter(
            root,
            store,
            product,
            TestingPrivateIngressRequest(
                context=context,
                endpoint_key=endpoint_key,
                edge_endpoint_key=receipt.edge_endpoint_key,
                access_list_id=host.access_list_id,
                certificate_id=host.certificate_id,
                reason="Verify testing health route without provider effects.",
            ),
            "testing-health-probe",
            client,
            route_activation=route_activation,
        )

        def unresolved_effects() -> bool:
            return any(
                reservation.provider_target_key == adapter.target_key()
                for reservation in store.list_held_provider_target_reservations()
            )

        if unresolved_effects():
            raise ValueError("Testing edge has unresolved effects.")
        # Reuse the full current ownership, placement, certificate, access-list,
        # alias and application-domain checks, using only the current host.
        snapshot, desired, current = adapter._snapshot()
        if (
            current is None
            or current.id != receipt.provider_host_id
            or not snapshot["domain_present"]
            or receipt.requested_domains != desired.domain_names
            or receipt.provider_route_sha256 != canonical_json_sha256(snapshot["existing"])
            or receipt.testing_authority_sha256
            != adapter._receipt_authority_sha256(cast(dict[str, object], snapshot["authority"]))
        ):
            raise ValueError("Private testing edge receipt changed.")
    except PublicHttpDestinationError:
        raise
    except (
        ValueError,
        FileNotFoundError,
        RuntimeError,
        OSError,
        SQLAlchemyError,
        click.ClickException,
    ):
        raise PublicHttpDestinationError(
            "private_url", "Private testing health route could not be verified."
        ) from None
    response = request_private_edge_health(
        url,
        edge_url=edge_origin[0],
        timeout_seconds=timeout_seconds,
    )
    try:
        if (
            os.environ.get(NPMPLUS_BASE_URL_ENV_KEY, "") != api_url
            or _edge_health_origin(client, api_url) != edge_origin
            or adapter._snapshot()[0] != snapshot
            or latest_testing_private_ingress_receipt(
                store, product=product, context=context, host_id=host.id
            )
            != receipt
            or store.read_private_health_endpoint_record(endpoint_key) != endpoint
            or unresolved_effects()
        ):
            raise ValueError("Testing health authority changed during the probe.")
    except PublicHttpDestinationError:
        raise
    except (
        ValueError,
        FileNotFoundError,
        RuntimeError,
        OSError,
        SQLAlchemyError,
        click.ClickException,
    ):
        raise PublicHttpDestinationError(
            "private_url", "Private testing health authority changed during the probe."
        ) from None
    return response
