from __future__ import annotations

from typing import Protocol

from control_plane.workflows.npmplus_ingress import (
    NpmplusIngressApplyRequest,
    NpmplusIngressApplyResult,
    NpmplusIngressClient,
    apply_npmplus_ingress_route,
)


class IngressProvider(Protocol):
    provider_id: str
    delegated_executor: str

    def apply_route(
        self,
        *,
        request: NpmplusIngressApplyRequest,
    ) -> NpmplusIngressApplyResult: ...


class NpmplusIngressProvider:
    provider_id = "npmplus"
    delegated_executor = "control-plane.npmplus"

    def __init__(self, *, client: NpmplusIngressClient) -> None:
        self._client = client

    def apply_route(
        self,
        *,
        request: NpmplusIngressApplyRequest,
    ) -> NpmplusIngressApplyResult:
        return apply_npmplus_ingress_route(client=self._client, request=request)
