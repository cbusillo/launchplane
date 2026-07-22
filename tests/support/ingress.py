from __future__ import annotations

from typing import Literal
from urllib.parse import urlencode

from fastapi import FastAPI
from httpx2 import Response

from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord, EdgeEndpointStatus
from control_plane.contracts.ingress_canary_route_record import IngressCanaryRouteRecord
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditOperation,
    IngressRouteAuditRecord,
)
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.npmplus import NpmplusProxyHost, NpmplusProxyHostPayload
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.workflows.npmplus_ingress import (
    NpmplusIngressApplyRequest,
    NpmplusIngressApplyResult,
    NpmplusIngressRouteDesiredState,
)
from tests.support.http import get as http_get


class _FakeNpmplusIngressClient:
    def __init__(self, proxy_hosts: tuple[NpmplusProxyHost, ...] = ()) -> None:
        self.proxy_hosts = list(proxy_hosts)
        self.calls: list[str] = []
        self.next_id = 100

    def list_proxy_hosts(self) -> tuple[NpmplusProxyHost, ...]:
        self.calls.append("list")
        return tuple(self.proxy_hosts)

    def create_proxy_host(self, payload: NpmplusProxyHostPayload) -> NpmplusProxyHost:
        self.calls.append("create")
        created = NpmplusProxyHost.model_validate({"id": self.next_id, **payload.to_api_payload()})
        self.proxy_hosts.append(created)
        return created

    def update_proxy_host(
        self, *, host_id: int, payload: NpmplusProxyHostPayload
    ) -> NpmplusProxyHost:
        self.calls.append(f"update:{host_id}")
        updated = NpmplusProxyHost.model_validate({"id": host_id, **payload.to_api_payload()})
        self.proxy_hosts = [updated if host.id == host_id else host for host in self.proxy_hosts]
        return updated

    def disable_proxy_host(self, host_id: int) -> NpmplusProxyHost:
        self.calls.append(f"disable:{host_id}")
        return self._set_enabled(host_id=host_id, enabled=False)

    def enable_proxy_host(self, host_id: int) -> NpmplusProxyHost:
        self.calls.append(f"enable:{host_id}")
        return self._set_enabled(host_id=host_id, enabled=True)

    def _set_enabled(self, *, host_id: int, enabled: bool) -> NpmplusProxyHost:
        for index, host in enumerate(self.proxy_hosts):
            if host.id == host_id:
                updated = NpmplusProxyHost.model_validate(
                    {**host.model_dump(mode="json"), "enabled": enabled}
                )
                self.proxy_hosts[index] = updated
                return updated
        raise AssertionError(f"Unknown proxy host: {host_id}")


class _FakeIngressProvider:
    provider_id = "fake-ingress"
    delegated_executor = "control-plane.fake-ingress"

    def __init__(self, result: NpmplusIngressApplyResult) -> None:
        self.result = result
        self.requests: list[NpmplusIngressApplyRequest] = []

    def apply_route(
        self,
        *,
        request: NpmplusIngressApplyRequest,
    ) -> NpmplusIngressApplyResult:
        self.requests.append(request)
        return self.result


def _npmplus_ingress_route_payload(
    *,
    mode: str = "dry-run",
    context: str = "reon-prod",
    instance: str = "",
    **overrides: object,
) -> dict[str, object]:
    route: dict[str, object] = {
        "domain_names": ["ingress-canary.example.test"],
        "forward_scheme": "http",
        "forward_host": "192.0.2.10",
        "forward_port": 8123,
        "certificate_id": 47,
    }
    route.update(overrides)
    return {
        "schema_version": 1,
        "product": "launchplane",
        "context": context,
        "instance": instance,
        "ingress": {
            "mode": mode,
            "route": route,
            "reason": "test ingress route apply",
        },
    }


def _npmplus_proxy_host(**overrides: object) -> NpmplusProxyHost:
    payload = NpmplusIngressRouteDesiredState(
        domain_names=("ingress-canary.example.test",),
        forward_scheme="https",
        forward_host="100.73.170.113",
        forward_port=443,
        certificate_id=47,
    ).to_proxy_host_payload()
    return NpmplusProxyHost.model_validate(
        {"id": 79, **payload.model_dump(mode="json"), "enabled": True, **overrides}
    )


def _private_health_endpoint_read_policy() -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["repairshopr-sync"],
                    "contexts": ["repairshopr-sync"],
                    "actions": ["private_health_endpoint.read"],
                }
            ]
        }
    )


def _ingress_route_audit_read_policy(*, contexts: tuple[str, ...]) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(
        {
            "github_actions": [
                {
                    "repository": "every/verireel",
                    "workflow_refs": [
                        "every/verireel/.github/workflows/preview-control-plane.yml@refs/heads/main"
                    ],
                    "event_names": ["pull_request"],
                    "products": ["launchplane"],
                    "contexts": list(contexts),
                    "actions": ["ingress_route.plan"],
                }
            ]
        }
    )


def _edge_endpoint_record(*, status: EdgeEndpointStatus = "active") -> EdgeEndpointRecord:
    return EdgeEndpointRecord(
        endpoint_key="cm-prod-dokploy",
        provider="dokploy",
        server_name="docker-cm-prod",
        upstream_host="100.73.170.113",
        upstream_host_kind="ip",
        upstream_scheme="https",
        upstream_port=443,
        status=status,
        updated_at="2026-06-07T00:00:00Z",
        source_label="test:edge-endpoint",
    )


def _edge_endpoint_apply_payload(*, mode: str = "dry-run") -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": mode,
        "endpoint": _edge_endpoint_record().model_dump(mode="json"),
        "reason": "test edge endpoint apply",
        "confirmation": "APPLY LAUNCHPLANE EDGE ENDPOINT" if mode == "apply" else "",
    }


def _private_health_endpoint_record(
    *, url: str = "http://10.0.0.5:8000/health"
) -> PrivateHealthEndpointRecord:
    return PrivateHealthEndpointRecord(
        endpoint_key="repairshopr-sync-prod-runtime",
        product="repairshopr-sync",
        context="repairshopr-sync",
        instance="prod",
        url=url,
        updated_at="2026-06-15T00:00:00Z",
        source_label="test:private-health-endpoint",
    )


def _private_health_endpoint_apply_payload(*, mode: str = "dry-run") -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": mode,
        "endpoint": _private_health_endpoint_record().model_dump(mode="json"),
        "reason": "test private health endpoint apply",
        "confirmation": "APPLY LAUNCHPLANE PRIVATE HEALTH ENDPOINT" if mode == "apply" else "",
    }


def _ingress_canary_route_record(*, status: str = "active") -> IngressCanaryRouteRecord:
    return IngressCanaryRouteRecord(
        canary_key="ingress-canary",
        product="launchplane",
        context="reon-prod",
        domain_name="ingress-canary.example.test",
        expected_host_id=78,
        edge_endpoint_key="cm-prod-dokploy",
        certificate_id=47,
        status=status,  # type: ignore[arg-type]
        updated_at="2026-06-11T00:00:00Z",
        source_label="test:ingress-canary-route",
    )


def _ingress_route_audit_record(
    *,
    record_id: str = "ingress-route-audit-test",
    product: str = "launchplane",
    context: str = "reon-prod",
    mode: Literal["dry-run", "apply"] = "dry-run",
    status: Literal["pending", "planned", "applied", "unchanged"] = "planned",
    dry_run: bool = True,
    provider_host_id: int | None = 78,
    trace_id: str = "trace-audit-1",
    idempotency_key: str = "audit-key-1",
    recorded_at: str = "2026-06-01T00:00:00Z",
) -> IngressRouteAuditRecord:
    return IngressRouteAuditRecord(
        record_id=record_id,
        product=product,
        context=context,
        mode=mode,
        status=status,
        dry_run=dry_run,
        requested_domains=("app.example.com",),
        edge_endpoint_key="edge-app",
        expected_host_id=None,
        provider_host_id=provider_host_id,
        operations=(
            IngressRouteAuditOperation(
                action="create",
                host_id=provider_host_id,
                domain_names=("app.example.com",),
                requires_apply=mode == "dry-run",
                change_categories=("create",),
            ),
        ),
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        reason="test",
        recorded_at=recorded_at,
    )


def _ingress_canary_route_record_apply_payload(*, mode: str = "dry-run") -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": mode,
        "route": _ingress_canary_route_record().model_dump(mode="json"),
        "reason": "test ingress canary route record apply",
        "confirmation": "APPLY LAUNCHPLANE INGRESS CANARY ROUTE RECORD" if mode == "apply" else "",
    }


def _authorization_headers(
    *,
    authorization: str,
    headers: dict[str, str] | None,
) -> dict[str, str]:
    request_headers = dict(headers or {})
    if authorization:
        request_headers["Authorization"] = authorization
    return request_headers


def _query_params(**values: str) -> dict[str, str]:
    return {name: value for name, value in values.items() if value}


async def _get_edge_endpoint_records(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    limit: str = "",
    provider: str = "",
    status: str = "",
) -> Response:
    params = _query_params(limit=limit, provider=provider, status=status)
    suffix = f"?{urlencode(params)}" if params else ""
    return await http_get(
        app,
        f"/v1/edge-endpoints/records{suffix}",
        headers=_authorization_headers(authorization=authorization, headers=headers),
    )


async def _get_edge_endpoint_record(
    app: FastAPI,
    endpoint_key: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> Response:
    return await http_get(
        app,
        f"/v1/edge-endpoints/records/{endpoint_key}",
        headers=_authorization_headers(authorization=authorization, headers=headers),
    )


async def _get_private_health_endpoint_records(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "repairshopr-sync",
    context: str = "repairshopr-sync",
    instance: str = "",
    status: str = "",
    limit: str = "",
) -> Response:
    params = _query_params(
        product=product,
        context=context,
        instance=instance,
        status=status,
        limit=limit,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await http_get(
        app,
        f"/v1/private-health-endpoints/records{suffix}",
        headers=_authorization_headers(authorization=authorization, headers=headers),
    )


async def _get_private_health_endpoint_record(
    app: FastAPI,
    endpoint_key: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "repairshopr-sync",
    context: str = "repairshopr-sync",
    instance: str = "prod",
) -> Response:
    params = _query_params(product=product, context=context, instance=instance)
    suffix = f"?{urlencode(params)}" if params else ""
    return await http_get(
        app,
        f"/v1/private-health-endpoints/records/{endpoint_key}{suffix}",
        headers=_authorization_headers(authorization=authorization, headers=headers),
    )


async def _get_route_binding_records(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "example-product",
    context: str = "example-testing",
    instance: str = "",
    status: str = "",
    limit: str = "",
) -> Response:
    params = _query_params(
        product=product,
        context=context,
        instance=instance,
        status=status,
        limit=limit,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await http_get(
        app,
        f"/v1/route-bindings/records{suffix}",
        headers=_authorization_headers(authorization=authorization, headers=headers),
    )


async def _get_route_binding_record(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "example-product",
    context: str = "example-testing",
    instance: str = "web",
) -> Response:
    params = _query_params(product=product, context=context, instance=instance)
    suffix = f"?{urlencode(params)}" if params else ""
    return await http_get(
        app,
        f"/v1/route-bindings/records/current{suffix}",
        headers=_authorization_headers(authorization=authorization, headers=headers),
    )


async def _get_ingress_canary_route_records(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "",
    context: str = "",
    status: str = "",
    limit: str = "",
) -> Response:
    params = _query_params(product=product, context=context, status=status, limit=limit)
    suffix = f"?{urlencode(params)}" if params else ""
    return await http_get(
        app,
        f"/v1/ingress/canary-routes/records{suffix}",
        headers=_authorization_headers(authorization=authorization, headers=headers),
    )


async def _get_ingress_canary_route_record(
    app: FastAPI,
    canary_key: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
) -> Response:
    return await http_get(
        app,
        f"/v1/ingress/canary-routes/records/{canary_key}",
        headers=_authorization_headers(authorization=authorization, headers=headers),
    )


async def _get_ingress_route_audit_records(
    app: FastAPI,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "",
    context: str = "",
    status: str = "",
    mode: str = "",
    provider_host_id: str = "",
    trace_id: str = "",
    idempotency_key: str = "",
    limit: str = "",
) -> Response:
    params = _query_params(
        product=product,
        context=context,
        status=status,
        mode=mode,
        provider_host_id=provider_host_id,
        trace_id=trace_id,
        idempotency_key=idempotency_key,
        limit=limit,
    )
    suffix = f"?{urlencode(params)}" if params else ""
    return await http_get(
        app,
        f"/v1/ingress/route-audits/records{suffix}",
        headers=_authorization_headers(authorization=authorization, headers=headers),
    )


async def _get_ingress_route_audit_record(
    app: FastAPI,
    record_id: str,
    *,
    authorization: str = "Bearer valid-token",
    headers: dict[str, str] | None = None,
    product: str = "",
    context: str = "",
) -> Response:
    params = _query_params(product=product, context=context)
    suffix = f"?{urlencode(params)}" if params else ""
    return await http_get(
        app,
        f"/v1/ingress/route-audits/records/{record_id}{suffix}",
        headers=_authorization_headers(authorization=authorization, headers=headers),
    )
