from __future__ import annotations

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


def build_launchplane_idempotency_record_id(*, response_trace_id: str) -> str:
    normalized_trace_id = response_trace_id.strip()
    if not normalized_trace_id:
        raise ValueError("idempotency record id requires response_trace_id")
    return f"idempotency-{normalized_trace_id}"
