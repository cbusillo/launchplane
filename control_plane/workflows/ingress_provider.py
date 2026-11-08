from __future__ import annotations

import os
from typing import Protocol

import click

from control_plane.npmplus import (
    NpmplusClient,
    NpmplusCredentials,
    NPMPLUS_BASE_URL_ENV_KEY as NPMPLUS_BASE_URL_ENV_KEY,
    NPMPLUS_IDENTITY_ENV_KEY as NPMPLUS_IDENTITY_ENV_KEY,
    NPMPLUS_SECRET_ENV_KEY as NPMPLUS_SECRET_ENV_KEY,
)
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

    def wildcard_client(self) -> NpmplusClient:
        if not isinstance(self._client, NpmplusClient):
            raise click.ClickException("Wildcard issuance requires the NPMplus service client.")
        return self._client

    def apply_route(
        self,
        *,
        request: NpmplusIngressApplyRequest,
    ) -> NpmplusIngressApplyResult:
        return apply_npmplus_ingress_route(client=self._client, request=request)


def build_npmplus_ingress_client_from_env() -> NpmplusClient:
    required_env = (
        NPMPLUS_BASE_URL_ENV_KEY,
        NPMPLUS_IDENTITY_ENV_KEY,
        NPMPLUS_SECRET_ENV_KEY,
    )
    missing_env = tuple(key for key in required_env if not os.environ.get(key, "").strip())
    if missing_env:
        missing_list = ", ".join(missing_env)
        raise click.ClickException(
            f"NPMplus ingress client is not configured; missing {missing_list}."
        )
    return NpmplusClient(
        credentials=NpmplusCredentials(
            base_url=os.environ.get(NPMPLUS_BASE_URL_ENV_KEY, ""),
            identity=os.environ.get(NPMPLUS_IDENTITY_ENV_KEY, ""),
            secret=os.environ.get(NPMPLUS_SECRET_ENV_KEY, ""),
        )
    )


def default_ingress_provider() -> IngressProvider:
    return NpmplusIngressProvider(client=build_npmplus_ingress_client_from_env())
