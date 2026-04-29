from __future__ import annotations

import hashlib
from typing import Any

from pydantic import BaseModel, Field


class LaunchplaneIdempotencyRecord(BaseModel):
    schema_version: int = 1
    record_id: str
    scope: str
    route_path: str
    idempotency_key: str
    request_fingerprint: str
    response_status_code: int = Field(ge=100, le=599)
    response_trace_id: str
    recorded_at: str
    response_payload: dict[str, Any]


def build_launchplane_idempotency_record_id(*, scope: str, route_path: str, request_token: str) -> str:
    normalized = f"{scope.strip()}\n{route_path.strip()}\n{request_token.strip()}"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    return f"idempotency-{digest}"
