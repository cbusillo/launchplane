"""Small, allowlisted metadata seam for GitHub quota response headers."""

from __future__ import annotations

from collections.abc import Callable, Mapping


GitHubResponseHeadersObserver = Callable[[Mapping[str, str]], None]

_GITHUB_QUOTA_RESPONSE_HEADERS = frozenset(
    {
        "retry-after",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
        "x-ratelimit-resource",
        "x-ratelimit-used",
    }
)


def normalized_github_quota_response_headers(headers: object) -> dict[str, str]:
    """Return only quota headers, with stable lowercase names."""
    items = getattr(headers, "items", None)
    if not callable(items):
        return {}
    try:
        pairs = items()
    except Exception:
        return {}
    normalized: dict[str, str] = {}
    try:
        for name, value in pairs:
            if not isinstance(name, str) or not isinstance(value, str):
                continue
            normalized_name = name.strip().lower()
            if normalized_name in _GITHUB_QUOTA_RESPONSE_HEADERS:
                normalized[normalized_name] = value.strip()
    except Exception:
        return {}
    return normalized


def notify_github_quota_response_headers(
    observer: GitHubResponseHeadersObserver | None,
    headers: object,
) -> None:
    """Best-effort observer notification that cannot alter HTTP behavior."""
    if observer is None:
        return
    try:
        observer(normalized_github_quota_response_headers(headers))
    except Exception:
        return
