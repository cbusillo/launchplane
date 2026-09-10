"""Extract and persist bounded ordinary-agent provider wait evidence."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from math import ceil
from typing import Protocol, TypeVar
from urllib.error import HTTPError

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentProviderQuotaKey,
    OrdinaryAgentProviderWaitObservation,
    OrdinaryAgentProviderWaitRecord,
)
from control_plane.github_response_headers import normalized_github_quota_response_headers


SECONDARY_RATE_LIMIT_FALLBACK_SECONDS = 60
MAX_PROVIDER_EPOCH = 2**63 - 1
MAX_CAUSE_DEPTH = 8


class OrdinaryAgentProviderWaitWriter(Protocol):
    def __call__(
        self,
        *,
        quota_key: OrdinaryAgentProviderQuotaKey,
        observation: OrdinaryAgentProviderWaitObservation,
    ) -> OrdinaryAgentProviderWaitRecord: ...


ProviderResponse = TypeVar("ProviderResponse")


def provider_wait_observation_from_exception(
    error: BaseException,
    *,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> OrdinaryAgentProviderWaitObservation | None:
    """Read explicit HTTP error causes; implicit unrelated exception contexts are ignored."""
    return _latest_observation(
        _provider_wait_observations_from_exception(
            error,
            now=_utc_datetime(utc_now()),
        )
    )


def provider_wait_observation_from_headers(
    headers: object,
    *,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> OrdinaryAgentProviderWaitObservation | None:
    """Parse a schedulable primary or secondary wait from quota response headers."""
    return _latest_observation(
        _provider_wait_observations_from_headers(
            headers,
            now=_utc_datetime(utc_now()),
        )
    )


def _provider_wait_observations_from_headers(
    headers: object,
    *,
    now: datetime,
) -> tuple[OrdinaryAgentProviderWaitObservation, ...]:
    normalized = normalized_github_quota_response_headers(headers)
    remaining = _header(normalized, "x-ratelimit-remaining")
    reset = _future_epoch(_header(normalized, "x-ratelimit-reset"), now=now)
    observations: list[OrdinaryAgentProviderWaitObservation] = []
    if remaining is not None and remaining.strip() == "0" and reset is not None:
        observations.append(
            OrdinaryAgentProviderWaitObservation(
                retry_not_before=reset,
                classification="primary_rate_limit",
            )
        )
    retry_not_before = _retry_after_epoch(_header(normalized, "retry-after"), now=now)
    if retry_not_before is not None:
        observations.append(
            OrdinaryAgentProviderWaitObservation(
                retry_not_before=retry_not_before,
                classification="secondary_rate_limit",
            )
        )
    return tuple(observations)


def provider_error_is_quota_limited(error: BaseException) -> bool:
    """Classify quota-shaped 403/429 errors without requiring a future deadline."""
    http_error = _chained_http_error(error)
    if http_error is None or http_error.code not in {403, 429}:
        return False
    if http_error.code == 429:
        return True
    headers = normalized_github_quota_response_headers(http_error.headers)
    remaining = _header(headers, "x-ratelimit-remaining")
    return (remaining is not None and remaining.strip() == "0") or "retry-after" in headers


def _provider_wait_observations_from_exception(
    error: BaseException,
    *,
    now: datetime,
) -> tuple[OrdinaryAgentProviderWaitObservation, ...]:
    http_error = _chained_http_error(error)
    if http_error is None or http_error.code not in {403, 429}:
        return ()
    observations = list(_provider_wait_observations_from_headers(http_error.headers, now=now))
    if http_error.code == 429 and not any(
        observation.classification == "secondary_rate_limit" for observation in observations
    ):
        fallback = _secondary_fallback(now=now)
        if fallback is not None:
            observations.append(fallback)
    return tuple(observations)


def provider_wait_observation_from_graphql(
    payload: object,
    *,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> OrdinaryAgentProviderWaitObservation | None:
    """Return quota evidence from an in-band GraphQL envelope without counting cost."""
    return _latest_observation(
        _provider_wait_observations_from_graphql(
            payload,
            now=_utc_datetime(utc_now()),
        )
    )


def _provider_wait_observations_from_graphql(
    payload: object,
    *,
    now: datetime,
) -> tuple[OrdinaryAgentProviderWaitObservation, ...]:
    if not isinstance(payload, Mapping):
        return ()
    observations: list[OrdinaryAgentProviderWaitObservation] = []
    data = payload.get("data")
    rate_limit = data.get("rateLimit") if isinstance(data, Mapping) else None
    if isinstance(rate_limit, Mapping) and _is_zero(rate_limit.get("remaining")):
        reset = _graphql_reset_epoch(rate_limit.get("resetAt"), now=now)
        if reset is not None:
            observations.append(
                OrdinaryAgentProviderWaitObservation(
                    retry_not_before=reset,
                    classification="primary_rate_limit",
                )
            )
    errors = payload.get("errors")
    if isinstance(errors, list) and any(
        isinstance(item, Mapping) and item.get("type") == "RATE_LIMITED" for item in errors
    ):
        fallback = _secondary_fallback(now=now)
        if fallback is not None:
            observations.append(fallback)
    return tuple(observations)


def observe_provider_wait_headers(
    headers: object,
    *,
    quota_key: OrdinaryAgentProviderQuotaKey,
    record_provider_wait: OrdinaryAgentProviderWaitWriter,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> OrdinaryAgentProviderWaitObservation | None:
    """Best-effort record every scoped wait carried by response headers."""
    try:
        observations = _provider_wait_observations_from_headers(
            headers,
            now=_utc_datetime(utc_now()),
        )
    except Exception:
        return None
    for observation in observations:
        _record_wait(
            quota_key=quota_key,
            observation=observation,
            record_provider_wait=record_provider_wait,
        )
    return _latest_observation(observations)


def observe_provider_wait_error(
    error: BaseException,
    *,
    quota_key: OrdinaryAgentProviderQuotaKey,
    record_provider_wait: OrdinaryAgentProviderWaitWriter,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> OrdinaryAgentProviderWaitObservation | None:
    """Best-effort record an HTTP wait while retaining typed evidence for the caller."""
    try:
        observations = _provider_wait_observations_from_exception(
            error,
            now=_utc_datetime(utc_now()),
        )
    except Exception:
        return None
    for observation in observations:
        _record_wait(
            quota_key=quota_key,
            observation=observation,
            record_provider_wait=record_provider_wait,
        )
    return _latest_observation(observations)


def observe_graphql_provider_wait(
    payload: object,
    *,
    quota_key: OrdinaryAgentProviderQuotaKey,
    record_provider_wait: OrdinaryAgentProviderWaitWriter,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> OrdinaryAgentProviderWaitObservation | None:
    """Best-effort record in-band GraphQL wait evidence and return the signal."""
    try:
        observations = _provider_wait_observations_from_graphql(
            payload,
            now=_utc_datetime(utc_now()),
        )
    except Exception:
        return None
    for observation in observations:
        _record_wait(
            quota_key=quota_key,
            observation=observation,
            record_provider_wait=record_provider_wait,
        )
    return _latest_observation(observations)


def call_with_provider_wait_observation(
    request: Callable[[], ProviderResponse],
    *,
    quota_key: OrdinaryAgentProviderQuotaKey,
    record_provider_wait: OrdinaryAgentProviderWaitWriter,
    utc_now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ProviderResponse:
    """Run one request, observe a quota failure, and re-raise its original exception."""
    try:
        return request()
    except Exception as error:
        try:
            observe_provider_wait_error(
                error,
                quota_key=quota_key,
                record_provider_wait=record_provider_wait,
                utc_now=utc_now,
            )
        except Exception:
            # Observer/clock failures cannot replace the original provider outcome.
            pass
        raise


def _latest_observation(
    observations: tuple[OrdinaryAgentProviderWaitObservation, ...],
) -> OrdinaryAgentProviderWaitObservation | None:
    return max(observations, key=lambda item: item.retry_not_before, default=None)


def _record_wait(
    *,
    quota_key: OrdinaryAgentProviderQuotaKey,
    observation: OrdinaryAgentProviderWaitObservation,
    record_provider_wait: OrdinaryAgentProviderWaitWriter,
) -> None:
    effective_quota_key = (
        quota_key.model_copy(update={"resource_class": "secondary"})
        if observation.classification == "secondary_rate_limit"
        else quota_key
    )
    try:
        record_provider_wait(quota_key=effective_quota_key, observation=observation)
    except Exception:
        # Shared wait persistence is advisory to the in-flight provider outcome.
        # It must never replace the provider exception or GraphQL evidence result.
        return


def _secondary_fallback(*, now: datetime) -> OrdinaryAgentProviderWaitObservation | None:
    epoch = ceil(now.timestamp()) + SECONDARY_RATE_LIMIT_FALLBACK_SECONDS
    if not _is_valid_future_epoch(epoch, now=now):
        return None
    return OrdinaryAgentProviderWaitObservation(
        retry_not_before=epoch, classification="secondary_rate_limit"
    )


def _chained_http_error(error: BaseException) -> HTTPError | None:
    current: BaseException | None = error
    seen: set[int] = set()
    for _ in range(MAX_CAUSE_DEPTH):
        if current is None or id(current) in seen:
            return None
        seen.add(id(current))
        if isinstance(current, HTTPError):
            return current
        current = current.__cause__
    return None


def _header(headers: object, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if not callable(getter):
        return None
    value = getter(name)
    return value if isinstance(value, str) else None


def _future_epoch(value: str | None, *, now: datetime) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized.isdecimal():
        return None
    try:
        epoch = int(normalized)
    except ValueError:
        return None
    return epoch if _is_valid_future_epoch(epoch, now=now) else None


def _retry_after_epoch(value: str | None, *, now: datetime) -> int | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized.isdecimal():
        try:
            delay = int(normalized)
        except ValueError:
            return None
        if delay <= 0:
            return None
        epoch = ceil(now.timestamp()) + delay
        return epoch if _is_valid_future_epoch(epoch, now=now) else None
    try:
        parsed = parsedate_to_datetime(normalized)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        epoch = ceil(parsed.astimezone(timezone.utc).timestamp())
    except (OverflowError, OSError, ValueError):
        return None
    return epoch if _is_valid_future_epoch(epoch, now=now) else None


def _graphql_reset_epoch(value: object, *, now: datetime) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    try:
        epoch = ceil(parsed.astimezone(timezone.utc).timestamp())
    except (OverflowError, OSError, ValueError):
        return None
    return epoch if _is_valid_future_epoch(epoch, now=now) else None


def _is_zero(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == 0


def _is_valid_future_epoch(epoch: int, *, now: datetime) -> bool:
    if not int(now.timestamp()) < epoch <= MAX_PROVIDER_EPOCH:
        return False
    try:
        datetime.fromtimestamp(epoch, timezone.utc)
    except (OverflowError, OSError, ValueError):
        return False
    return True


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("provider wait clock must be timezone-aware")
    return value.astimezone(timezone.utc)
