from __future__ import annotations

import hashlib
import json


CANONICAL_JSON_INTEGER_MIN = -(2**63)
CANONICAL_JSON_INTEGER_MAX = 2**63 - 1


def _validate_canonical_json_value(payload: object, *, location: str = "$") -> None:
    if payload is None or isinstance(payload, (bool, str)):
        return
    if isinstance(payload, int):
        if not CANONICAL_JSON_INTEGER_MIN <= payload <= CANONICAL_JSON_INTEGER_MAX:
            raise ValueError(f"canonical JSON integer at {location} must fit signed 64-bit range")
        return
    if isinstance(payload, float):
        raise ValueError(f"canonical JSON number at {location} must be an integer")
    if isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            _validate_canonical_json_value(item, location=f"{location}[{index}]")
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            if not isinstance(key, str):
                raise ValueError(f"canonical JSON object key at {location} must be a string")
            _validate_canonical_json_value(value, location=f"{location}.{key}")
        return
    raise TypeError(f"unsupported canonical JSON value at {location}: {type(payload).__name__}")


def canonical_json_bytes(payload: object) -> bytes:
    """Return the public canonical UTF-8 JSON representation for a JSON payload."""

    _validate_canonical_json_value(payload)
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_sha256(payload: object) -> str:
    """Return the lowercase SHA-256 digest of public canonical JSON bytes."""

    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
