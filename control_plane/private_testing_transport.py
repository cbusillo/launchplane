"""Inert preparation of a lane-local Nginx HTTPS/WSS listener.

This module reads current target evidence. It never installs trust, reads a key,
changes Compose, creates an endpoint record, or deploys a workload.
"""

from copy import deepcopy
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path, PurePosixPath
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.dokploy import api, compose, source
from control_plane.generic_web_testing_exclusion import _bind
from control_plane.storage.postgres import PostgresRecordStore

ROUTE = "/v1/products/{product}/private-testing-transport/plan"


class PrivateTestingTransportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    mode: Literal["dry-run", "readback"] = "dry-run"
    endpoint_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    private_ip: IPv4Address
    listener_port: int = Field(ge=1024, le=65535)
    runtime_service: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,62}$")
    nginx_image: str = Field(pattern=r"^[a-z0-9][a-z0-9./:_-]+@sha256:[a-f0-9]{64}$")
    ca_bundle_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    certificate_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    monitor_ca_bundle_path: str
    receiver_ca_bundle_path: str
    trust_review: str = Field(min_length=1, max_length=1000)
    expected_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")

    @field_validator("private_ip")
    @classmethod
    def _private_address(cls, value: IPv4Address) -> IPv4Address:
        # is_private also includes non-routable documentation and loopback space.
        if not any(
            value in IPv4Network(network)
            for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        ):
            raise ValueError("Listener requires a private RFC1918 address.")
        return value

    @field_validator("monitor_ca_bundle_path", "receiver_ca_bundle_path")
    @classmethod
    def _bundle_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not path.is_absolute() or ".." in path.parts or any(c in value for c in "\r\n\x00"):
            raise ValueError("CA bundle requires an absolute non-traversing path.")
        return value


class PrivateTestingTransportRefused(ValueError):
    """Evidence is incomplete or differs from the reviewed preparation."""


def nginx_configuration(*, service: str, runtime_port: int) -> str:
    """Preserve URI and authorization through precisely two routes."""
    return f"""pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events {{}}
http {{
    access_log off;
    server {{
        listen 8443 ssl;
        ssl_certificate /run/secrets/private_tls_certificate;
        ssl_certificate_key /run/secrets/private_tls_key;
        ssl_protocols TLSv1.2 TLSv1.3;
        location = /health {{
            proxy_pass http://{service}:{runtime_port};
        }}
        location = /agent-session/connect {{
            proxy_pass http://{service}:{runtime_port};
            proxy_http_version 1.1;
            proxy_set_header Upgrade $http_upgrade;
            proxy_set_header Connection "upgrade";
            proxy_read_timeout 3600s;
        }}
        location / {{ return 404; }}
    }}
}}
"""


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise PrivateTestingTransportRefused("Compose evidence must be a complete object.")
    return value


def transport_overlay(
    request: PrivateTestingTransportRequest, *, namespace: str, runtime_port: int
) -> dict[str, object]:
    return {
        "services": {
            namespace: {
                "image": request.nginx_image,
                "user": "65534:65534",
                "entrypoint": ["nginx", "-g", "daemon off;"],
                "read_only": True,
                "cap_drop": ["ALL"],
                "security_opt": ["no-new-privileges:true"],
                "tmpfs": ["/tmp", "/var/cache/nginx"],
                "ports": [
                    {
                        "host_ip": str(request.private_ip),
                        "published": str(request.listener_port),
                        "target": 8443,
                        "protocol": "tcp",
                    }
                ],
                "configs": [{"source": namespace, "target": "/etc/nginx/nginx.conf"}],
                "secrets": [
                    {"source": namespace + "-certificate", "target": "private_tls_certificate"},
                    {"source": namespace + "-key", "target": "private_tls_key"},
                ],
            }
        },
        "configs": {
            namespace: {
                "content": nginx_configuration(
                    service=request.runtime_service, runtime_port=runtime_port
                )
            }
        },
        "secrets": {
            namespace + "-certificate": {"file": f"./{namespace}/server.crt"},
            namespace + "-key": {"file": f"./{namespace}/server.key"},
        },
    }


def _baseline(
    current: dict[str, object], overlay: dict[str, object], *, readback: bool
) -> dict[str, object]:
    baseline = deepcopy(current)
    for section, additions in overlay.items():
        actual = _object(baseline.get(section, {}))
        for name, expected in _object(additions).items():
            if readback:
                if actual.get(name) != expected:
                    raise PrivateTestingTransportRefused("Private listener readback differs.")
                del actual[name]
            elif name in actual:
                raise PrivateTestingTransportRefused("Private listener resource already exists.")
        if actual:
            baseline[section] = actual
        else:
            baseline.pop(section, None)
    return baseline


def _validate_compose(baseline: dict[str, object], runtime_service: str) -> None:
    services = _object(baseline.get("services"))
    if runtime_service not in services:
        raise PrivateTestingTransportRefused("Testing runtime service is not prepared.")
    if baseline.get("networks"):
        raise PrivateTestingTransportRefused(
            "Preparation requires the target's own default network."
        )
    volumes = _object(baseline.get("volumes", {}))
    for volume in volumes.values():
        # A compose-scoped volume has no explicit global name/host driver options.
        if volume not in (None, {}):
            raise PrivateTestingTransportRefused("Testing storage is not compose-scoped.")
    if baseline.get("secrets") or baseline.get("configs"):
        raise PrivateTestingTransportRefused(
            "Existing external configuration custody is unqualified."
        )
    for raw_service in services.values():
        service = _object(raw_service)
        if any(
            service.get(key)
            for key in (
                "ports",
                "network_mode",
                "networks",
                "privileged",
                "external_links",
                "volumes_from",
                "devices",
                "container_name",
                "extends",
                "labels",
            )
        ):
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
    ):
        raise PrivateTestingTransportRefused(
            "Fresh provider identity/private scope is unqualified."
        )
    rendered = compose.fetch_dokploy_converted_compose_file(
        host=host, token=token, compose_id=target_id.target_id
    )
    namespace = (
        "lp-private-tls-"
        + canonical_json_sha256(
            {"product": product, "context": request.context, "instance": request.instance}
        )[:16]
    )
    overlay = transport_overlay(request, namespace=namespace, runtime_port=profile.runtime_port)
    try:
        current = _object(yaml.safe_load(rendered))
    except yaml.YAMLError as error:
        raise PrivateTestingTransportRefused("Provider Compose evidence is invalid.") from error
    baseline = _baseline(current, overlay, readback=request.mode == "readback")
    _validate_compose(baseline, request.runtime_service)
    # Recheck records after both provider reads. A changed lane needs a fresh plan.
    if _bind(store, product, request.context, request.instance) != records:
        raise PrivateTestingTransportRefused("Testing target changed during preparation.")
    binding = canonical_json_sha256(
        {
            "records": [record.model_dump(mode="json") for record in records],
            "placement": {key: placement[key] for key in ("composeId", "serverId", "appName")},
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
    authority = f"{request.private_ip}:{request.listener_port}"
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
        "endpoint": {
            "endpoint_key": request.endpoint_key,
            "product": product,
            "context": request.context,
            "instance": request.instance,
            "url": f"https://{authority}/health",
        },
        "server_url": f"wss://{authority}/agent-session/connect",
        "trust": {
            "issuer": "private",
            "required_ip_san": str(request.private_ip),
            "ca_bundle_sha256": request.ca_bundle_sha256,
            "certificate_sha256": request.certificate_sha256,
            "monitor_environment": {"SSL_CERT_FILE": request.monitor_ca_bundle_path},
            "receiver_environment": {"SSL_CERT_FILE": request.receiver_ca_bundle_path},
            "review": request.trust_review,
        },
        "activation_prerequisites": [
            "separately_authorized_testing_activation",
            "fresh_host_bind_and_port_exclusion",
            "private_network_reachability_and_no_public_forwarding",
            "owner_qualified_certificate_key_and_ca_bundle_custody",
            "reviewed_original_operation_and_readback",
            "live_https_and_authenticated_wss",
        ],
    }
