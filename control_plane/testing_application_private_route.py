"""Private-edge ownership follows the original receipt and native activation chain."""

import json
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from control_plane.contracts.testing_activation import TestingActivationRecord
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.dokploy import source
from control_plane.dokploy.testing_route import observe_testing_route
from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport


class PrivateIngressAuthorityChanged(ValueError):
    """A complete current edge or record contradicts original private authority."""


def require_private_ingress_records(
    store: Any, product: str, context: str, binding: dict[str, str]
) -> None:
    from control_plane.testing_private_ingress import latest_testing_private_ingress_receipt

    if any(
        row.provider_target_key == f"testing-private-ingress:{product}:{context}"
        for row in store.list_held_provider_target_reservations()
    ):
        raise ValueError("The private testing ingress has unresolved effects.")
    endpoint = store.read_private_health_endpoint_record(binding["endpoint_key"])
    audit = store.read_ingress_route_audit_record(binding["receipt_id"])
    edge = store.read_edge_endpoint_record(audit.edge_endpoint_key)
    digest = canonical_json_sha256(
        {
            "endpoint": endpoint.model_dump(mode="json"),
            "receipt": audit.model_dump(mode="json"),
            "edge": edge.model_dump(mode="json"),
        }
    )
    if (
        digest != binding["records_sha256"]
        or audit.provider_host_id is None
        or latest_testing_private_ingress_receipt(
            store, product=product, context=context, host_id=audit.provider_host_id
        )
        != audit
    ):
        raise PrivateIngressAuthorityChanged("The original private ingress records changed.")


def published_application_lineage(
    store: Any, product: str, context: str, target_id: str, hostname: str
) -> tuple[str, tuple[TestingActivationRecord, ...]]:
    from control_plane.testing_activation import completed_application_activation

    records = [
        TestingActivationRecord.model_validate(value)
        for value in store.list_testing_application_activations(product, context)
    ]
    chain: list[TestingActivationRecord] = []
    visited: set[str] = set()
    while True:
        if target_id in visited:
            raise ValueError("The private application lineage is cyclic.")
        visited.add(target_id)
        matches = [
            record
            for record in records
            if record.target_id == target_id and completed_application_activation(store, record)
        ]
        if not matches:
            break
        if len(matches) != 1:
            raise ValueError("The private application lineage is ambiguous.")
        record = matches[0]
        readiness = record.application_readiness
        if (
            readiness is None
            or readiness.route is None
            or readiness.route.host != hostname
            or record.published_route_binding is not None
            or not record.retained_private_ingress
        ):
            raise ValueError("The completed private application route is unavailable.")
        chain.append(record)
        target_id = record.retained_target_id.target_id
    return target_id, tuple(chain)


def application_route_transport(
    root: Path, record: TestingActivationRecord
) -> DokployTestingRouteTransport:
    readiness = record.application_readiness
    assert readiness is not None and readiness.route is not None
    route, identity, execution = (
        readiness.route,
        readiness.old_runtime_identity,
        readiness.route_execution,
    )
    predecessor = record.predecessor
    if (
        identity is None
        or execution is None
        or record.request.application is None
        or predecessor is None
        or predecessor.runtime_identity != identity
    ):
        raise ValueError("The original private native route custody is unavailable.")
    backend = urlsplit(route.old_backend)
    host, token = source.read_dokploy_config(control_plane_root=root)
    transport = DokployTestingRouteTransport(
        host=host,
        token=token,
        server_id=execution.server_id,
        old_project="",
        product=record.product,
        context=record.context,
        public_host=route.host,
        entrypoints=route.public_entrypoints,
        old_runtime_identity=identity,
        old_application=None
        if record.published
        else {
            "name": backend.hostname,
            "port": backend.port,
            "product": record.product,
            "context": record.context,
            "deployments": {
                identity.deployment_record_id: {
                    "artifact": identity.artifact_id,
                    "source": identity.source_git_ref,
                }
            },
            "route_file_name": route.application_file_name,
            "service_id": predecessor.service_id,
            "task_template_sha256": predecessor.task_template_sha256,
        },
        new_application={
            "name": record.app_name,
            "port": record.request.application.runtime_port,
            "service_id": readiness.service_id,
            "task_id": readiness.task_id,
        },
    )
    transport.payload["expected_application_file_sha256"] = route.application_file_sha256
    return transport


def require_application_route_selected(root: Path, record: TestingActivationRecord) -> None:
    readiness = record.application_readiness
    assert readiness is not None and readiness.route is not None
    transport = application_route_transport(root, record)
    frame = transport.read()
    owned = json.loads(frame["owned_file"]) if frame["owned_file"] is not None else None
    admitted = {readiness.backend}
    if not record.published:
        # The caller supplies only original activation progress; physical effects
        # still own their normal native fence. A pending forward child can be
        # observed at either of its exact admitted endpoints.
        admitted.add(readiness.route.old_backend)
    if observe_testing_route(readiness.route, frame["raw"], owned) not in admitted:
        raise ValueError("The private native route does not select the canonical application.")


def private_ingress_snapshot(
    root: Path,
    store: Any,
    product: str,
    context: str,
    *,
    endpoint_key: str = "",
    route_activation: TestingActivationRecord | None = None,
) -> tuple[dict[str, Any], Any, dict[str, str]]:
    from control_plane.testing_private_ingress import (
        TestingPrivateIngressAdapter,
        TestingPrivateIngressRequest,
        latest_testing_private_ingress_receipt,
    )
    from control_plane.workflows.ingress_provider import build_npmplus_ingress_client_from_env

    from control_plane.testing_public_ingress import _host_payload

    endpoints = [
        endpoint
        for endpoint in store.list_private_health_endpoint_records()
        if (endpoint.product, endpoint.context, endpoint.instance, endpoint.status)
        == (product, context, "testing", "active")
        and (not endpoint_key or endpoint.endpoint_key == endpoint_key)
    ]
    if len(endpoints) != 1:
        raise ValueError("Use one exact active private testing health endpoint.")
    endpoint = endpoints[0]
    hostname = urlsplit(endpoint.url).hostname
    client = build_npmplus_ingress_client_from_env()
    hosts = [host for host in client.list_proxy_hosts() if hostname in host.domain_names]
    if len(hosts) != 1:
        raise ValueError("Use the unique original private testing edge.")
    host = client.get_proxy_host(hosts[0].id, public_only=True)
    receipt = latest_testing_private_ingress_receipt(
        store, product=product, context=context, host_id=host.id
    )
    if (
        receipt is None
        or not host.enabled
        or not host.access_list_id
        or not isinstance(host.certificate_id, int)
        or host.certificate_id < 1
    ):
        raise PrivateIngressAuthorityChanged(
            "The original private testing ingress is not enabled and owned."
        )
    if receipt.provider_route_sha256 != canonical_json_sha256(_host_payload(host)):
        raise PrivateIngressAuthorityChanged("The original private edge payload changed.")
    from control_plane.testing_public_ingress import _certificate_current

    certificate = client.get_certificate(host.certificate_id)
    if not _certificate_current(certificate):
        raise PrivateIngressAuthorityChanged("The original private edge certificate expired.")
    adapter = TestingPrivateIngressAdapter(
        root,
        store,
        product,
        TestingPrivateIngressRequest(
            context=context,
            endpoint_key=endpoint.endpoint_key,
            edge_endpoint_key=receipt.edge_endpoint_key,
            access_list_id=host.access_list_id,
            certificate_id=host.certificate_id,
            reason="Verify original private application route custody.",
        ),
        "application-route-authority",
        client,
        route_activation=route_activation,
    )
    if any(
        row.provider_target_key == adapter.target_key()
        for row in store.list_held_provider_target_reservations()
    ):
        raise ValueError("The private testing ingress has unresolved effects.")
    snapshot, desired, current = adapter._snapshot()
    if (
        current is None
        or current.id != receipt.provider_host_id
        or not snapshot["domain_present"]
        or receipt.requested_domains != desired.domain_names
        or receipt.provider_route_sha256 != canonical_json_sha256(_host_payload(current))
        or receipt.testing_authority_sha256
        != adapter._receipt_authority_sha256(cast(dict[str, object], snapshot["authority"]))
    ):
        raise ValueError("The original private testing ingress receipt changed.")
    binding = {
        "endpoint_key": endpoint.endpoint_key,
        "server_id": str(cast(dict[str, object], snapshot["authority"])["server_id"]),
        "receipt_id": receipt.record_id,
        "snapshot_sha256": canonical_json_sha256(
            {"snapshot": snapshot, "receipt": receipt.model_dump(mode="json")}
        ),
        "records_sha256": canonical_json_sha256(
            {
                "endpoint": endpoint.model_dump(mode="json"),
                "receipt": receipt.model_dump(mode="json"),
                "edge": store.read_edge_endpoint_record(receipt.edge_endpoint_key).model_dump(
                    mode="json"
                ),
            }
        ),
    }
    return snapshot, adapter, binding
