from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import logging
from time import monotonic
from typing import Literal, cast
from urllib.error import HTTPError
from urllib.parse import urlsplit

from control_plane.contracts.github_app_installation_token import GitHubAppInstallationToken
from control_plane.github_response_headers import normalized_github_quota_response_headers

_LOGGER = logging.getLogger(__name__)


def configure_github_request_logging() -> None:
    """Route tally INFO records to stderr without enabling other libraries' INFO."""
    logging.basicConfig()
    _LOGGER.setLevel(logging.INFO)


SLOW_GITHUB_REQUEST_SECONDS = 5.0
SLOW_GITHUB_OPERATION_SECONDS = 60.0
MAX_QUOTA_OBSERVATIONS = 8
GRAPHQL_INT_MAX = 2_147_483_647
GitHubRequestPhase = Literal["operation", "controller", "admission", "feedback"]


@dataclass
class _GitHubResponseEvidence:
    phase: GitHubRequestPhase
    host: str | None
    app_id: int | None
    installation_id: int | None
    authenticated: bool
    graphql: bool
    status: int | None = None
    headers: dict[str, str] = field(default_factory=dict)
    graphql_points: int | None = None


_ACTIVE_RESPONSE: ContextVar[_GitHubResponseEvidence | None] = ContextVar(
    "launchplane_github_response_evidence", default=None
)
_ACTIVE_PHASE: ContextVar[GitHubRequestPhase] = ContextVar(
    "launchplane_github_request_phase", default="operation"
)


def _quota_number(value: str | None) -> int | None:
    if value is not None and value.isascii() and value.isdecimal() and len(value) <= 19:
        return int(value)
    return None


@contextmanager
def github_request_phase(phase: GitHubRequestPhase) -> Iterator[None]:
    token = _ACTIVE_PHASE.set(phase)
    try:
        yield
    finally:
        _ACTIVE_PHASE.reset(token)


def _record_quota_observation(
    tally: _GitHubRequestTally, response: _GitHubResponseEvidence
) -> None:
    headers = response.headers
    resource = headers.get("x-ratelimit-resource")
    if resource not in {"core", "search", "graphql", "code_search", "integration_manifest"}:
        resource = None
    metadata: dict[str, object] = {
        "phase": response.phase,
        "host": response.host,
        "app_id": response.app_id,
        "installation_id": response.installation_id,
        "status": response.status,
        "resource": resource,
        "reset": _quota_number(headers.get("x-ratelimit-reset")),
        "charged_requests": 0
        if response.status == 304 and response.authenticated and not response.graphql
        else "UNKNOWN",
    }
    key = tuple(metadata.values())
    observation = tally.quota_observations.get(key)
    if observation is None:
        if len(tally.quota_observations) >= MAX_QUOTA_OBSERVATIONS:
            tally.quota_omitted_requests += 1
            return
        observation = {**metadata, "requests": 0, "graphql_points": 0}
        tally.quota_observations[key] = observation
    points = observation["graphql_points"]
    observation["graphql_points"] = (
        points + response.graphql_points
        if isinstance(points, int) and response.graphql_points is not None
        else "UNKNOWN"
    )
    counters = {
        name: _quota_number(headers.get(f"x-ratelimit-{name}"))
        for name in ("used", "remaining", "limit")
    }
    observation.setdefault("first", counters)
    observation["last"] = counters
    observation["requests"] = cast(int, observation["requests"]) + 1


@dataclass
class _GitHubRequestTally:
    credential: GitHubAppInstallationToken | None = field(default=None, repr=False)
    quota_observations: dict[tuple[object, ...], dict[str, object]] = field(default_factory=dict)
    quota_omitted_requests: int = 0
    count: int = 0
    not_modified: int = 0
    seconds: float = 0.0
    slowest_seconds: float = 0.0
    slowest_request: str = ""


_ACTIVE_TALLY: ContextVar[_GitHubRequestTally | None] = ContextVar(
    "launchplane_github_request_tally", default=None
)


def _request_label(*, method: str, path: str) -> str:
    return f"{method.upper()} {path.split('?', 1)[0]}"


def record_github_response(status: int, headers: object = None) -> None:
    response = _ACTIVE_RESPONSE.get()
    if response is not None:
        response.status = status if type(status) is int and 100 <= status <= 599 else None
        response.headers = normalized_github_quota_response_headers(headers)


def record_github_graphql_cost(payload: object) -> None:
    response = _ACTIVE_RESPONSE.get()
    if response is None or not response.graphql or response.status != 200:
        return
    if not isinstance(payload, dict) or payload.get("errors"):
        return
    data = payload.get("data")
    rate_limit = data.get("rateLimit") if isinstance(data, dict) else None
    cost = rate_limit.get("cost") if isinstance(rate_limit, dict) else None
    if type(cost) is int and 0 < cost <= GRAPHQL_INT_MAX:
        response.graphql_points = cost


@contextmanager
def timed_github_request(
    *,
    method: str,
    path: str,
    token: str | GitHubAppInstallationToken | None = None,
    api_base_url: str = "https://api.github.com",
    clock: Callable[[], float] = monotonic,
) -> Iterator[None]:
    started = clock()
    tally = _ACTIVE_TALLY.get()
    credential = token if isinstance(token, GitHubAppInstallationToken) else None
    if credential is None and tally is not None and tally.credential is not None:
        if token == tally.credential.token:
            credential = tally.credential
    host = urlsplit(api_base_url).hostname
    response = _GitHubResponseEvidence(
        phase=_ACTIVE_PHASE.get(),
        host=host if host and len(host) <= 253 else None,
        app_id=credential.app_id if credential else None,
        installation_id=credential.installation_id if credential else None,
        authenticated=bool(token),
        graphql=path.split("?", 1)[0] == "/graphql",
    )
    response_token = _ACTIVE_RESPONSE.set(response)
    try:
        yield
    except HTTPError as error:
        record_github_response(error.code, error.headers)
        raise
    finally:
        _ACTIVE_RESPONSE.reset(response_token)
        elapsed = clock() - started
        label = _request_label(method=method, path=path)
        tally = _ACTIVE_TALLY.get()
        if tally is not None:
            tally.count += 1
            tally.not_modified += int(response.status == 304)
            _record_quota_observation(tally, response)
            tally.seconds += elapsed
            if elapsed > tally.slowest_seconds:
                tally.slowest_seconds = elapsed
                tally.slowest_request = label
        if elapsed >= SLOW_GITHUB_REQUEST_SECONDS:
            _LOGGER.warning("Slow GitHub API request: %s took %.1fs", label, elapsed)


@contextmanager
def github_request_tally(
    operation: str,
    *,
    credential: str | GitHubAppInstallationToken | None = None,
    phase: GitHubRequestPhase = "controller",
    clock: Callable[[], float] = monotonic,
) -> Iterator[None]:
    """Log each operation's requests, including conditional responses and failures."""
    tally = _GitHubRequestTally(
        credential=credential if isinstance(credential, GitHubAppInstallationToken) else None
    )
    token = _ACTIVE_TALLY.set(tally)
    phase_token = _ACTIVE_PHASE.set(phase)
    started = clock()
    try:
        yield
    finally:
        _ACTIVE_TALLY.reset(token)
        _ACTIVE_PHASE.reset(phase_token)
        elapsed = clock() - started
        _LOGGER.info(
            "GitHub request tally: %s; requests=%d not_modified=%d non_304_requests=%d seconds=%.1f",
            operation,
            tally.count,
            tally.not_modified,
            tally.count - tally.not_modified,
            tally.seconds,
        )
        for observation in tally.quota_observations.values():
            _LOGGER.info(
                "GitHub quota observation: %s; %s",
                operation,
                json.dumps(observation, separators=(",", ":")),
            )
        if tally.quota_omitted_requests:
            _LOGGER.info(
                "GitHub quota coverage: %s; omitted_requests=%d charged_requests=UNKNOWN graphql_points=UNKNOWN",
                operation,
                tally.quota_omitted_requests,
            )
        if elapsed >= SLOW_GITHUB_OPERATION_SECONDS:
            _LOGGER.warning(
                "Slow operation: %s took %.1fs; %d GitHub requests took %.1fs, slowest %s at %.1fs",
                operation,
                elapsed,
                tally.count,
                tally.seconds,
                tally.slowest_request or "none",
                tally.slowest_seconds,
            )
