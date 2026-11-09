from __future__ import annotations

from typing import Protocol

import click

from control_plane import secrets
from control_plane.npmplus import (
    NpmplusClient,
    NpmplusCredentials,
    NPMPLUS_BASE_URL_ENV_KEY,
    NPMPLUS_IDENTITY_ENV_KEY,
    NPMPLUS_SECRET_ENV_KEY,
    validate_npmplus_base_url,
)
from control_plane.runtime_environments import SiteRuntimeEnvironmentStore
from control_plane.service_runtime_settings import SERVICE_CONTEXT
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.npmplus_ingress import (
    NpmplusIngressApplyRequest,
    NpmplusIngressApplyResult,
    NpmplusIngressClient,
    apply_npmplus_ingress_route,
)


def npmplus_service_base_url_from_records(*, record_store: SiteRuntimeEnvironmentStore) -> str:
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
        return validate_npmplus_base_url(base_url)
    except ValueError:
        raise click.ClickException("NPMplus service endpoint is invalid.") from None


def build_npmplus_ingress_client_from_records(
    *, record_store: SiteRuntimeEnvironmentStore
) -> NpmplusClient:
    base_url = npmplus_service_base_url_from_records(record_store=record_store)
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


def default_ingress_provider(*, record_store: object) -> NpmplusIngressProvider:
    if not isinstance(record_store, PostgresRecordStore):
        raise click.ClickException("NPMplus service credentials require database records.")
    return NpmplusIngressProvider(
        client=build_npmplus_ingress_client_from_records(record_store=record_store)
    )
