from __future__ import annotations

import os
from typing import Protocol
from urllib.parse import urlsplit

import click

from control_plane import secrets
from control_plane.npmplus import NpmplusClient, NpmplusCredentials
from control_plane.runtime_environments import RuntimeEnvironmentRecordStore
from control_plane.service_runtime_settings import SERVICE_CONTEXT
from control_plane.workflows.npmplus_ingress import (
    NpmplusIngressApplyRequest,
    NpmplusIngressApplyResult,
    NpmplusIngressClient,
    apply_npmplus_ingress_route,
)


NPMPLUS_BASE_URL_ENV_KEY = "LAUNCHPLANE_NPMPLUS_BASE_URL"
NPMPLUS_IDENTITY_ENV_KEY = "LAUNCHPLANE_NPMPLUS_IDENTITY"
NPMPLUS_SECRET_ENV_KEY = "LAUNCHPLANE_NPMPLUS_SECRET"


class IngressCredentialStore(RuntimeEnvironmentRecordStore, secrets.SecretReadStore, Protocol):
    pass


def build_npmplus_ingress_client_from_records(
    *, record_store: IngressCredentialStore
) -> NpmplusClient:
    records = tuple(
        record
        for record in record_store.list_runtime_environment_records(context_name=SERVICE_CONTEXT)
        if record.scope == "context" and record.context == SERVICE_CONTEXT and not record.instance
    )
    if len(records) != 1:
        raise click.ClickException("NPMplus requires one exact Launchplane service runtime record.")
    base_url = records[0].env.get(NPMPLUS_BASE_URL_ENV_KEY)
    if not isinstance(base_url, str) or not base_url.strip():
        raise click.ClickException("NPMplus service endpoint is missing or invalid.")
    try:
        parsed = urlsplit(base_url.strip())
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        _ = parsed.port
    except ValueError:
        raise click.ClickException("NPMplus service endpoint is invalid.") from None
    values = {
        key: secrets.resolve_context_secret_value_from_store(
            record_store=record_store,
            integration=secrets.LAUNCHPLANE_SERVICE_SECRET_INTEGRATION,
            context_name=SERVICE_CONTEXT,
            binding_key=key,
        )
        for key in (NPMPLUS_IDENTITY_ENV_KEY, NPMPLUS_SECRET_ENV_KEY)
    }
    if not all(values.values()):
        raise click.ClickException(
            "NPMplus requires unique configured service credential bindings."
        )
    return NpmplusClient(
        credentials=NpmplusCredentials(
            base_url=base_url,
            identity=values[NPMPLUS_IDENTITY_ENV_KEY],
            secret=values[NPMPLUS_SECRET_ENV_KEY],
        )
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
