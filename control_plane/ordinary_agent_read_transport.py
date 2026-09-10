"""Shared custody-scoped transport accounting for ordinary read attempts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Literal, Protocol

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentProviderQuotaKey,
    OrdinaryAgentProviderWaitObservation,
    OrdinaryAgentProviderWaitRecord,
)
from control_plane.contracts.ordinary_agent_snapshot import OrdinaryAgentProviderRequestCounts
from control_plane.github_app_identity import GitHubApiRequest
from control_plane.ordinary_agent_custody import OrdinaryAgentCustodyCleanupUnknown
from control_plane.ordinary_agent_github_transport import (
    DeadlineMergeTrainGitHubTransport,
    OrdinaryAgentProviderDeferred,
    OrdinaryAgentProviderEvidenceError,
)
from control_plane.ordinary_agent_github_transport import ordinary_provider_resource_class
from control_plane.ordinary_agent_quota_transport import observed_ordinary_api_request


class OrdinaryAgentReadWaitStore(Protocol):
    def record_provider_wait(
        self,
        *,
        quota_key: OrdinaryAgentProviderQuotaKey,
        observation: OrdinaryAgentProviderWaitObservation,
    ) -> OrdinaryAgentProviderWaitRecord: ...


class OrdinaryAgentReadApiTransport:
    def __init__(
        self,
        *,
        token: str,
        api_request: GitHubApiRequest,
        installation_id: int,
        store: OrdinaryAgentReadWaitStore,
        utc_now: Callable[[], datetime],
    ) -> None:
        self._token = token
        self._api_request = api_request
        self._installation_id = installation_id
        self._store = store
        self._utc_now = utc_now

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        values: dict[str, object] = {"method": method, "path": path, "token": self._token}
        if body is not None:
            values["body"] = body
        return observed_ordinary_api_request(
            api_request=self._api_request,
            quota_key=OrdinaryAgentProviderQuotaKey(
                authority_kind="installation",
                authority_id=self._installation_id,
                resource_class=ordinary_provider_resource_class(path),
            ),
            writer=self._store.record_provider_wait,
            utc_now=self._utc_now,
            **values,
        )


def ordinary_agent_read_request_counts(
    transport: DeadlineMergeTrainGitHubTransport | None,
) -> OrdinaryAgentProviderRequestCounts:
    return OrdinaryAgentProviderRequestCounts(
        rest_core_requests=transport.rest_core_requests if transport else 0,
        graphql_requests=transport.graphql_requests if transport else 0,
        graphql_points=transport.graphql_points if transport else 0,
    )


def ordinary_agent_read_failure_reason(
    error: Exception,
) -> Literal[
    "provider_wait",
    "provider_attempt_deadline",
    "provider_incomplete",
    "provider_transport",
    "snapshot_query_cost_exceeded",
    "cleanup_unknown",
]:
    if isinstance(error, OrdinaryAgentCustodyCleanupUnknown):
        return "cleanup_unknown"
    if isinstance(error, OrdinaryAgentProviderDeferred):
        return _ordinary_agent_read_reason_or_incomplete(error.reason_code)
    if isinstance(error, OrdinaryAgentProviderEvidenceError):
        return _ordinary_agent_read_reason_or_incomplete(error.reason_code)
    return "provider_transport"


def _ordinary_agent_read_reason_or_incomplete(
    reason_code: str,
) -> Literal[
    "provider_wait",
    "provider_attempt_deadline",
    "provider_incomplete",
    "provider_transport",
    "snapshot_query_cost_exceeded",
    "cleanup_unknown",
]:
    if reason_code == "provider_wait":
        return "provider_wait"
    if reason_code == "provider_attempt_deadline":
        return "provider_attempt_deadline"
    if reason_code == "snapshot_query_cost_exceeded":
        return "snapshot_query_cost_exceeded"
    if reason_code == "provider_transport":
        return "provider_transport"
    if reason_code == "cleanup_unknown":
        return "cleanup_unknown"
    return "provider_incomplete"
