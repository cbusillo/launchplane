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
    if not isinstance(value, int) or isinstance(value, bool):
        raise error_type(message)
    return value


def required_positive_int(
    value: object,
    message: str,
    *,
    error_type: Callable[[str], Exception],
) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise error_type(message)
    return value
