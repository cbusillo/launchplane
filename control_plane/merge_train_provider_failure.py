"""Sanitized provider evidence shared by planning receipts and feedback retries."""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.error import HTTPError, URLError


@dataclass(frozen=True)
class ProviderFailure:
    cause: str | None = None
    retryable: bool = False
    rate_limited: bool = False
    status_code: int | None = None
    remaining: str = ""
    retry_after: str = ""
    reset: str = ""
    request_id: str = ""


def provider_failure(error: BaseException) -> ProviderFailure:
    """Consume a bounded secondary-quota body once, on the original HTTP error."""
    current: BaseException | None = error
    seen: set[int] = set()
    network = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, HTTPError):
            cached = getattr(current, "_merge_train_provider_failure", None)
            if isinstance(cached, ProviderFailure):
                return cached
            headers = current.headers
            remaining = headers.get("x-ratelimit-remaining", "") if headers else ""
            retry_after = headers.get("retry-after", "") if headers else ""
            secondary = False
            if current.code == 403 and remaining != "0" and not retry_after:
                try:
                    payload = json.loads(current.read(4096))
                    message = payload.get("message") if isinstance(payload, dict) else None
                    secondary = (
                        isinstance(message, str) and "secondary rate limit" in message.casefold()
                    )
                except (OSError, TypeError, ValueError):
                    pass
            limited = current.code == 429 or (
                current.code == 403 and (remaining == "0" or bool(retry_after) or secondary)
            )
            cause = (
                "rate_limited"
                if limited
                else "invalid_credentials"
                if current.code == 401
                else "permission_denied"
                if current.code == 403
                else "transient_provider_failure"
                if current.code >= 500
                else "provider_rejected"
            )
            result = ProviderFailure(
                cause=cause,
                retryable=limited or current.code >= 500,
                rate_limited=limited,
                status_code=current.code,
                remaining=remaining,
                retry_after=retry_after,
                reset=headers.get("x-ratelimit-reset", "") if headers else "",
                request_id=headers.get("x-github-request-id", "") if headers else "",
            )
            setattr(current, "_merge_train_provider_failure", result)
            return result
        network = network or isinstance(current, (URLError, OSError))
        current = current.__cause__
    return (
        ProviderFailure(cause="transient_provider_failure", retryable=True)
        if network
        else ProviderFailure()
    )
