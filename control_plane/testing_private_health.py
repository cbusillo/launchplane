"""Health-only transport through a freshly verified testing edge route."""

import os
from pathlib import Path
from typing import cast

import click
from sqlalchemy.exc import SQLAlchemyError

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ingress_route_audit_record import TESTING_PRIVATE_INGRESS_AUDIT_PREFIX
from control_plane.contracts.private_health_endpoint_record import (
    private_health_endpoint_record_sha256,
)
from control_plane.outbound_http import (
    PublicHttpDestinationError,
    PublicHttpResponse,
    request_private_edge_health,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_private_ingress import (
    TestingPrivateIngressAdapter,
    TestingPrivateIngressRequest,
)
from control_plane.workflows.ingress_provider import (
    NPMPLUS_BASE_URL_ENV_KEY,
    build_npmplus_ingress_client_from_env,
)


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
        client = build_npmplus_ingress_client_from_env()
        receipts = tuple(
            receipt
            for receipt in store.list_ingress_route_audit_records(
                product=product, context_name=context
            )
            if receipt.record_id.startswith(TESTING_PRIVATE_INGRESS_AUDIT_PREFIX)
            and receipt.product == product
            and receipt.context == context
        )
        if any(receipt.status == "pending" for receipt in receipts):
            raise ValueError("Testing edge has unresolved effects.")
        for receipt in receipts:
            if (
                receipt.status != "applied"
                or receipt.mode != "apply"
                or receipt.dry_run
                or receipt.provider != "npmplus"
                or receipt.tls_owner != "provider"
                or receipt.provider_host_id is None
            ):
                continue
            host = client.get_proxy_host(receipt.provider_host_id, public_only=True)
            if (
                not host.enabled
                or host.access_list_id < 1
                or not isinstance(host.certificate_id, int)
                or host.certificate_id < 1
            ):
                continue
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
            )
            # Reuse the route operation's full current ownership, placement,
            # certificate, access-list, alias and application-domain checks.
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
                continue
            # Registration and readback do not prove transport. Only this real
            # GET, with ordinary TLS verification, can produce an observation.
            break
        else:
            raise ValueError("No current private testing edge receipt.")
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
        edge_api_url=os.environ.get(NPMPLUS_BASE_URL_ENV_KEY, ""),
        timeout_seconds=timeout_seconds,
    )
    try:
        if (
            adapter._snapshot()[0] != snapshot
            or store.read_ingress_route_audit_record(receipt.record_id) != receipt
            or store.read_private_health_endpoint_record(endpoint_key) != endpoint
        ):
            raise ValueError("Testing health authority changed during the probe.")
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
