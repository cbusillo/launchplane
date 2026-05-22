from __future__ import annotations

from collections.abc import Callable


def json_object(
    value: object,
    label: str,
    *,
    error_type: Callable[[str], Exception],
) -> dict[str, object]:
    if not isinstance(value, dict):
        raise error_type(f"{label} must be a JSON object.")
    return value


def required_stripped_text(
    value: object,
    message: str,
    *,
    error_type: Callable[[str], Exception],
) -> str:
    normalized_value = str(value or "").strip()
    if not normalized_value:
        raise error_type(message)
    return normalized_value


def required_string_text(
    value: object,
    message: str,
    *,
    error_type: Callable[[str], Exception],
) -> str:
    if not isinstance(value, str):
        raise error_type(message)
    normalized_value = value.strip()
    if not normalized_value:
        raise error_type(message)
    return normalized_value


def required_int(
    value: object,
    message: str,
    *,
    error_type: Callable[[str], Exception],
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise error_type(message)
    return value


def required_positive_int(
    value: object,
    message: str,
    *,
    error_type: Callable[[str], Exception],
) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise error_type(message)
    return value


def repository_full_name(repository: str) -> str:
    normalized_repository = "/".join(part.strip() for part in repository.strip().split("/"))
    if normalized_repository.count("/") != 1:
        raise ValueError("GitHub repository must be formatted as owner/name.")
    owner, repo = normalized_repository.split("/", 1)
    if not owner or not repo:
        raise ValueError("GitHub repository must be formatted as owner/name.")
    return normalized_repository
