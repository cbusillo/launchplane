"""Bounded GitHub transport primitives for ordinary-agent provider attempts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Literal

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentProviderQuotaKey,
    OrdinaryAgentProviderWaitRecord,
)

from control_plane.github_payload import json_object
from control_plane.merge_train_github import MergeTrainGitHubTransport


ORDINARY_PROVIDER_TRANSPORT_ALLOWANCE_SECONDS = 15
ORDINARY_SNAPSHOT_WORK_SECONDS = 90
ORDINARY_CANDIDATE_CHECK_WORK_SECONDS = 45
ORDINARY_MUTATION_WORK_SECONDS = 75
MAX_GRAPHQL_POINTS_PER_QUERY = 10


class OrdinaryAgentProviderDeferred(RuntimeError):
    """Closed pre-request deferral safe for persistence and public projection."""

    def __init__(
        self, reason_code: str = "provider_attempt_deadline", *, retry_not_before: int | None = None
    ) -> None:
        self.retry_not_before = retry_not_before
        super().__init__(reason_code)
        self.reason_code = reason_code


class OrdinaryAgentProviderEvidenceError(RuntimeError):
    """Closed malformed/incomplete provider-evidence failure."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def ordinary_provider_resource_class(path: str) -> Literal["core", "graphql"]:
    # Finite ordinary operations use REST core or GraphQL; none issue search requests.
    return "graphql" if path == "/graphql" else "core"


class DeadlineMergeTrainGitHubTransport:
    def __init__(
        self,
        *,
        transport: MergeTrainGitHubTransport,
        work_deadline: float,
        token_deadline: float,
        monotonic: Callable[[], float],
    ) -> None:
        self._transport = transport
        self._work_deadline = work_deadline
        self._token_deadline = token_deadline
        self._monotonic = monotonic
        self.rest_core_requests = 0
        self.graphql_requests = 0
        self.graphql_points = 0

    def request(
        self,
        *,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        minimum_remaining_seconds: float = ORDINARY_PROVIDER_TRANSPORT_ALLOWANCE_SECONDS,
    ) -> object:
        self.require_remaining(minimum_remaining_seconds)
        if ordinary_provider_resource_class(path) == "graphql":
            self.graphql_requests += 1
        else:
            self.rest_core_requests += 1
        return self._transport.request(method=method, path=path, body=body)

    def require_remaining(self, minimum_seconds: float) -> None:
        """Reserve time for subsequent phases without extending either deadline."""
        if not ORDINARY_PROVIDER_TRANSPORT_ALLOWANCE_SECONDS <= minimum_seconds < float("inf"):
            raise ValueError(
                "provider minimum remaining time must be finite and at least 15 seconds"
            )
        remaining = min(self._work_deadline, self._token_deadline) - self._monotonic()
        if remaining < minimum_seconds:
            raise OrdinaryAgentProviderDeferred()

    def record_graphql_points(self, points: int) -> None:
        if points < 0:
            raise OrdinaryAgentProviderEvidenceError("graphql_cost_invalid")
        self.graphql_points += points
        if points > MAX_GRAPHQL_POINTS_PER_QUERY:
            raise OrdinaryAgentProviderEvidenceError("snapshot_query_cost_exceeded")


def require_complete_graphql_data(
    payload: object, *, transport: DeadlineMergeTrainGitHubTransport
) -> dict[str, object]:
    envelope = json_object(
        payload,
        "GitHub GraphQL response",
        error_type=lambda message: OrdinaryAgentProviderEvidenceError("graphql_response_malformed"),
    )
    data = envelope.get("data")
    # A partial response can still consume quota. Account for reported cost
    # before rejecting its evidence; never treat an error as a free request.
    errors = envelope.get("errors")
    if errors not in (None, []):
        rate_limit = data.get("rateLimit") if isinstance(data, dict) else None
        cost = rate_limit.get("cost") if isinstance(rate_limit, dict) else None
        if isinstance(cost, int) and not isinstance(cost, bool) and cost >= 0:
            transport.graphql_points += cost
        if (
            isinstance(errors, list)
            and errors
            and all(
                isinstance(error, dict) and error.get("type") == "RATE_LIMITED" for error in errors
            )
        ):
            raise OrdinaryAgentProviderEvidenceError("provider_wait")
        raise OrdinaryAgentProviderEvidenceError("graphql_field_error")
    if not isinstance(data, dict):
        raise OrdinaryAgentProviderEvidenceError("graphql_required_data_missing")
    rate_limit = data.get("rateLimit")
    if not isinstance(rate_limit, dict):
        raise OrdinaryAgentProviderEvidenceError("graphql_rate_limit_missing")
    cost = rate_limit.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, int):
        raise OrdinaryAgentProviderEvidenceError("graphql_rate_limit_missing")
    transport.record_graphql_points(cost)
    return data


def require_complete_connection(value: object, *, label: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, dict):
        raise OrdinaryAgentProviderEvidenceError(f"{label}_missing")
    nodes = value.get("nodes")
    page_info = value.get("pageInfo")
    total_count = value.get("totalCount")
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise OrdinaryAgentProviderEvidenceError(f"{label}_malformed")
    if page_info.get("hasNextPage") is not False:
        raise OrdinaryAgentProviderEvidenceError(f"{label}_truncated")
    if (
        isinstance(total_count, bool)
        or not isinstance(total_count, int)
        or total_count != len(nodes)
    ):
        raise OrdinaryAgentProviderEvidenceError(f"{label}_truncated")
    if any(not isinstance(node, dict) for node in nodes):
        raise OrdinaryAgentProviderEvidenceError(f"{label}_malformed")
    return tuple(node for node in nodes if isinstance(node, dict))


ProviderWaitReader = Callable[..., OrdinaryAgentProviderWaitRecord | None]
ProviderResourceClass = Literal["core", "search", "graphql", "secondary"]


def require_installation_provider_ready(
    *,
    app_id: int,
    installation_id: int,
    resource_classes: tuple[ProviderResourceClass, ...],
    read_provider_wait: ProviderWaitReader,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> None:
    """Refuse mint while any discovered installation quota wait is active."""
    now_epoch = int(utc_now().astimezone(timezone.utc).timestamp())
    authorities: tuple[tuple[Literal["app", "installation"], int], ...] = (
        ("app", app_id),
        ("installation", installation_id),
    )
    retry_not_before = now_epoch
    for authority_kind, authority_id in authorities:
        for resource_class in resource_classes:
            wait = read_provider_wait(
                quota_key=OrdinaryAgentProviderQuotaKey(
                    authority_kind=authority_kind,
                    authority_id=authority_id,
                    resource_class=resource_class,
                )
            )
            if wait is not None:
                retry_not_before = max(retry_not_before, wait.retry_not_before)
    if retry_not_before > now_epoch:
        raise OrdinaryAgentProviderDeferred("provider_wait", retry_not_before=retry_not_before)
