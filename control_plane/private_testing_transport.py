"""Inert preparation of a private HTTP upstream for an existing TLS edge.

This module reads current target evidence. It never installs trust, reads a key,
changes Compose, creates an endpoint record, or deploys a workload.
"""

from copy import deepcopy
from ipaddress import IPv4Address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.dokploy import api, compose, source
from control_plane.generic_web_testing_exclusion import _bind
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.outbound_http import is_rfc1918_address, private_url_error

ROUTE = "/v1/products/{product}/private-testing-transport/plan"
PRODUCT_PATTERN = r"^[a-z0-9][a-z0-9_-]*$"


class PrivateTestingTransportIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    endpoint_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    private_ip: IPv4Address
    listener_port: int = Field(ge=1024, le=65535)
    runtime_service: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    endpoint_url: str

    @field_validator("private_ip")
    @classmethod
    def _private_address(cls, value: IPv4Address) -> IPv4Address:
        if not is_rfc1918_address(value):
            raise ValueError("Upstream bind requires a private RFC1918 address.")
        return value

    @field_validator("endpoint_url")
    @classmethod
    def _endpoint(cls, value: str) -> str:
        if private_url_error(value):
            raise ValueError("Edge endpoint must allow only private destinations.")
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.path != "/health" or parsed.query or parsed.fragment:
            raise ValueError("Edge endpoint requires HTTPS /health without query or fragment.")
        return value


class PrivateTestingTransportRefused(ValueError):
    """Evidence is incomplete or differs from the reviewed preparation."""


class PrivateTestingTransportRequest(PrivateTestingTransportIntent):
    mode: Literal["dry-run", "readback"] = "dry-run"
    expected_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PrivateTestingTransportRefused("Compose evidence must be a complete object.")
    return value


def transport_overlay(
    request: PrivateTestingTransportIntent, *, runtime_port: int
) -> dict[str, object]:
    """Publish the existing runtime on one private interface; TLS stays at the edge."""
    return {
        "services": {
            request.runtime_service: {
                "ports": [
                    {
                        "host_ip": str(request.private_ip),
                        "published": str(request.listener_port),
                        "target": runtime_port,
                        "protocol": "tcp",
                    }
                ]
            }
        }
    }


def _baseline(
    current: dict[str, object], overlay: dict[str, object], *, readback: bool
) -> dict[str, object]:
    baseline = deepcopy(current)
    services = _object(baseline.get("services"))
    for name, additions in _object(overlay["services"]).items():
        service = _object(services.get(name))
        expected_ports = _object(additions)["ports"]
        if "ports" in service:
            if service["ports"] != expected_ports:
                raise PrivateTestingTransportRefused("Private upstream readback differs.")
            del service["ports"]
        elif readback:
            raise PrivateTestingTransportRefused("Private upstream is absent.")
    return baseline


def _without_provider_metadata(
    current: dict[str, object],
    *,
    target_id: str,
    app_name: str,
    overlay: dict[str, object],
) -> dict[str, object]:
    current = deepcopy(current)
    name = current.pop("name", app_name)
    if name != app_name:
        raise PrivateTestingTransportRefused("Provider Compose names a different project.")
    services = _object(current.get("services"))
    for service_name, raw_service in services.items():
        service = _object(raw_service)
        labels = service.pop("labels", {})
        if isinstance(labels, list):
            if any(not isinstance(label, str) or "=" not in label for label in labels):
                raise PrivateTestingTransportRefused("Provider labels are unqualified.")
            labels = dict(label.split("=", 1) for label in labels)
        labels = _object(labels)
        expected = _object(_object(overlay["services"]).get(service_name, {}))
        expected_labels = _object(expected.get("labels", {}))
        owned_labels = {
            key: labels.pop(key)
            for key, value in expected_labels.items()
            if labels.get(key) == value
        }
        if owned_labels:
            service["labels"] = owned_labels
        if labels and (
            set(labels) - compose.DOKPLOY_COMPOSE_METADATA_LABELS
            or labels.get("dokploy.application.id") != target_id
            or labels.get("dokploy.service") != service_name
        ):
            raise PrivateTestingTransportRefused("Provider metadata names another target or route.")
    networks = current.get("networks")
    # Like testing activation, ignore only Dokploy's unused root declaration.
    # An attachment, alias, other declaration or provider option remains refused.
    if networks == {"dokploy-network": {"external": True}} and all(
        not _object(service).get("networks") for service in services.values()
    ):
        current.pop("networks")
    return current


def _validate_compose(baseline: dict[str, object], runtime_service: str) -> None:
    services = _object(baseline.get("services"))
    if runtime_service not in services:
        raise PrivateTestingTransportRefused("Testing runtime service is not prepared.")
    if set(baseline) - {"services", "volumes"}:
        raise PrivateTestingTransportRefused(
            "Preparation requires the target's own default network."
        )
    volumes = _object(baseline.get("volumes", {}))
    for volume in volumes.values():
        # A compose-scoped volume has no explicit global name/host driver options.
        if volume not in (None, {}):
            raise PrivateTestingTransportRefused("Testing storage is not compose-scoped.")
    for raw_service in services.values():
        service = _object(raw_service)
        if set(service) - {
            "image",
            "environment",
            "entrypoint",
            "command",
            "volumes",
            "depends_on",
            "expose",
            "healthcheck",
            "user",
            "working_dir",
            "restart",
            "init",
            "read_only",
            "stop_grace_period",
            "stop_signal",
            "cpus",
            "mem_limit",
            "pids_limit",
            "shm_size",
        }:
            raise PrivateTestingTransportRefused(
                "Testing service network/isolation is unqualified."
            )
        mounts = service.get("volumes", [])
        if not isinstance(mounts, list):
            raise PrivateTestingTransportRefused("Testing mounts are unqualified.")
        for mount in mounts:
            if not isinstance(mount, str) or mount.split(":", 1)[0] not in volumes:
                raise PrivateTestingTransportRefused(
                    "Testing mount is not a target-local named volume."
                )


def plan_private_testing_transport(
    store: PostgresRecordStore,
    *,
    root: Path,
    product: str,
    request: PrivateTestingTransportRequest,
) -> dict[str, object]:
    records = _bind(store, product, request.context, request.instance)
    profile, target, target_id, _provider = records
    if (
        not profile.is_active
        or profile.public_website != "none"
        or profile.health_path != "/health"
        or not profile.runtime_port
        or target.domains
        or target.public_hosts
        or target.pending_public_host_effects
    ):
        raise PrivateTestingTransportRefused(
            "Preparation requires a private no-website testing service."
        )
    host, token = source.read_dokploy_config(control_plane_root=root)
    placement = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=target_id.target_id
    )
    if (
        placement.get("composeId") != target_id.target_id
        or not placement.get("serverId")
        or not placement.get("appName")
        or placement.get("domains") != []
        or placement.get("composeType") != "docker-compose"
        or placement.get("isolatedDeployment") is not False
        or placement.get("randomize") is not False
        or placement.get("serviceNetworks") != []
        or placement.get("mounts") != []
    ):
        raise PrivateTestingTransportRefused(
            "Fresh provider identity/private scope is unqualified."
        )
    rendered = compose.fetch_dokploy_converted_compose_file(
        host=host, token=token, compose_id=target_id.target_id
    )
    overlay = transport_overlay(request, runtime_port=profile.runtime_port)
    try:
        current = _object(yaml.safe_load(rendered))
    except yaml.YAMLError as error:
        raise PrivateTestingTransportRefused("Provider Compose evidence is invalid.") from error
    current = _without_provider_metadata(
        current,
        target_id=target_id.target_id,
        app_name=str(placement["appName"]),
        overlay=overlay,
    )
    baseline = _baseline(current, overlay, readback=request.mode == "readback")
    _validate_compose(baseline, request.runtime_service)
    # Recheck records after both provider reads. A changed lane needs a fresh plan.
    if _bind(store, product, request.context, request.instance) != records:
        raise PrivateTestingTransportRefused("Testing target changed during preparation.")
    binding = canonical_json_sha256(
        {
            "records": [record.model_dump(mode="json") for record in records],
            "placement": {
                key: placement[key]
                for key in (
                    "composeId",
                    "serverId",
                    "appName",
                    "composeType",
                    "isolatedDeployment",
                    "randomize",
                    "serviceNetworks",
                    "mounts",
                )
            },
            "provider_host": host,
            "compose": baseline,
        }
    )
    inputs = request.model_dump(mode="json", exclude={"mode", "expected_plan_sha256"})
    digest = canonical_json_sha256({"binding": binding, "request": inputs, "overlay": overlay})
    if (request.expected_plan_sha256 and request.expected_plan_sha256 != digest) or (
        request.mode == "readback" and not request.expected_plan_sha256
    ):
        raise PrivateTestingTransportRefused("Private listener review is absent or stale.")
    return {
        "product": product,
        "context": request.context,
        "instance": request.instance,
        "plan_sha256": digest,
        "binding_sha256": binding,
        "applied": False,
        "mode": request.mode,
        "configuration_matches": request.mode == "readback",
        "transport_verified": False,
        "certificate_verified": False,
        "activation_supported": False,
        "overlay": overlay,
        "endpoint_proposal": {
            "endpoint_key": request.endpoint_key,
            "product": product,
            "context": request.context,
            "instance": request.instance,
            "url": request.endpoint_url,
        },
        "server_url": edge_server_url(request.endpoint_url),
        "tls_termination": "edge",
        "activation_prerequisites": [
            "separately_authorized_testing_activation",
            "fresh_host_bind_and_port_exclusion",
            "private_network_reachability_and_no_public_forwarding",
            "edge_certificate_hostname_and_chain",
            "edge_private_access_and_testing_only_upstream",
            "upstream_restricted_to_edge",
            "reviewed_original_operation_and_readback",
            "live_https_and_authenticated_wss",
        ],
    }


def edge_server_url(endpoint_url: str) -> str:
    parsed = urlsplit(endpoint_url)
    return urlunsplit(("wss", parsed.netloc, "/agent-session/connect", "", ""))
