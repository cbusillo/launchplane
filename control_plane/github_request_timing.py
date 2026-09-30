"""Timing evidence for GitHub API calls made on behalf of one service request.

The service does not log requests, so a controller call that spends minutes in
GitHub leaves nothing to read. These helpers log a single slow GitHub request,
and summarize the GitHub time of a long-running operation, without recording
request bodies, headers, or query strings.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import logging
from time import monotonic

_LOGGER = logging.getLogger(__name__)

SLOW_GITHUB_REQUEST_SECONDS = 5.0
SLOW_GITHUB_OPERATION_SECONDS = 60.0


@dataclass
class _GitHubRequestTally:
    count: int = 0
    seconds: float = 0.0
    slowest_seconds: float = 0.0
    slowest_request: str = ""


_ACTIVE_TALLY: ContextVar[_GitHubRequestTally | None] = ContextVar(
    "launchplane_github_request_tally", default=None
)


def _request_label(*, method: str, path: str) -> str:
    return f"{method.upper()} {path.split('?', 1)[0]}"


@contextmanager
def timed_github_request(
    *,
    method: str,
    path: str,
    clock: Callable[[], float] = monotonic,
) -> Iterator[None]:
    started = clock()
    try:
        yield
    finally:
        elapsed = clock() - started
        label = _request_label(method=method, path=path)
        tally = _ACTIVE_TALLY.get()
        if tally is not None:
            tally.count += 1
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
    clock: Callable[[], float] = monotonic,
) -> Iterator[None]:
    """Log how a long operation's time split between GitHub and everything else."""
    tally = _GitHubRequestTally()
    token = _ACTIVE_TALLY.set(tally)
    started = clock()
    try:
        yield
    finally:
        _ACTIVE_TALLY.reset(token)
        elapsed = clock() - started
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
