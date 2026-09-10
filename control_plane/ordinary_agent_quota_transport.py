"""Observe scoped provider responses without gating an in-flight operation."""

from collections.abc import Callable, Mapping
from datetime import datetime, timezone

from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentProviderQuotaKey
from control_plane.github_app_identity import GitHubApiRequest
from control_plane.merge_train_github import (
    MergeTrainGitHubTransport,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.ordinary_agent_github_transport import ordinary_provider_resource_class
from control_plane.ordinary_agent_provider_wait import (
    OrdinaryAgentProviderWaitWriter,
    observe_graphql_provider_wait,
    observe_provider_wait_error,
    observe_provider_wait_headers,
)
from control_plane.workflows.launchplane import github_api_request


def observe_quota_response_headers(
    headers: Mapping[str, str],
    *,
    quota_key: OrdinaryAgentProviderQuotaKey,
    writer: OrdinaryAgentProviderWaitWriter,
    utc_now: Callable[[], datetime],
) -> None:
    observe_provider_wait_headers(
        headers, quota_key=quota_key, record_provider_wait=writer, utc_now=utc_now
    )


def _observe_request(
    request: Callable[[], object],
    *,
    quota_key: OrdinaryAgentProviderQuotaKey,
    writer: OrdinaryAgentProviderWaitWriter,
    utc_now: Callable[[], datetime],
) -> object:
    try:
        payload = request()
    except Exception as error:
        observe_provider_wait_error(
            error, quota_key=quota_key, record_provider_wait=writer, utc_now=utc_now
        )
        raise
    if quota_key.resource_class == "graphql":
        observe_graphql_provider_wait(
            payload, quota_key=quota_key, record_provider_wait=writer, utc_now=utc_now
        )
    return payload


def observed_ordinary_api_request(
    *,
    api_request: GitHubApiRequest,
    quota_key: OrdinaryAgentProviderQuotaKey,
    writer: OrdinaryAgentProviderWaitWriter,
    utc_now: Callable[[], datetime],
    **kwargs: object,
) -> object:
    values = dict(kwargs)
    if api_request is github_api_request:
        values["response_headers_observer"] = lambda headers: observe_quota_response_headers(
            headers, quota_key=quota_key, writer=writer, utc_now=utc_now
        )
    # Injected API fakes retain body/error observation; the production default
    # also supplies actual response headers without exposing token payloads.
    return _observe_request(
        lambda: api_request(**values), quota_key=quota_key, writer=writer, utc_now=utc_now
    )


class OrdinaryAgentQuotaTransport:
    def __init__(
        self,
        *,
        token: str,
        installation_id: int,
        writer: OrdinaryAgentProviderWaitWriter,
        transport: MergeTrainGitHubTransport | None = None,
        utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._token = token
        self._installation_id = installation_id
        self._writer = writer
        self._transport = transport
        self._utc_now = utc_now

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        key = OrdinaryAgentProviderQuotaKey(
            authority_kind="installation",
            authority_id=self._installation_id,
            resource_class=ordinary_provider_resource_class(path),
        )
        # Constructing the default transport performs no I/O. A per-request
        # callback binds its resource key without mutable observer context.
        transport = self._transport
        if transport is None:
            transport = UrllibMergeTrainGitHubTransport(
                token=self._token,
                response_headers_observer=lambda headers: observe_quota_response_headers(
                    headers, quota_key=key, writer=self._writer, utc_now=self._utc_now
                ),
            )
        return _observe_request(
            lambda: transport.request(method=method, path=path, body=body),
            quota_key=key,
            writer=self._writer,
            utc_now=self._utc_now,
        )
