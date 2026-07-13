from __future__ import annotations

from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord, EdgeEndpointStatus
from control_plane.contracts.ingress_canary_route_record import IngressCanaryRouteRecord
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.npmplus import NpmplusProxyHost, NpmplusProxyHostPayload
from control_plane.workflows.npmplus_ingress import (
    NpmplusIngressApplyRequest,
    NpmplusIngressApplyResult,
    NpmplusIngressRouteDesiredState,
)


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
    *, mode: str = "dry-run", context: str = "reon-prod", **overrides: object
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


def _ingress_canary_route_record_apply_payload(*, mode: str = "dry-run") -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": mode,
        "route": _ingress_canary_route_record().model_dump(mode="json"),
        "reason": "test ingress canary route record apply",
        "confirmation": "APPLY LAUNCHPLANE INGRESS CANARY ROUTE RECORD" if mode == "apply" else "",
    }
