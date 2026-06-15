from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict


StructuredHealthStatus = Literal["unchecked", "pass", "fail", "malformed"]


class StructuredHealthEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: StructuredHealthStatus = "unchecked"
    reported_status: str = ""
    version: str = ""
    source_git_ref: str = ""
    image_reference: str = ""
    detail: str = ""
    components: tuple[str, ...] = ()


_PASS_STATUSES = {"ok", "pass", "passing", "ready", "healthy", "serving", "up"}
_FAIL_STATUSES = {"fail", "failed", "error", "errored", "not_ready", "unhealthy", "down"}


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _component_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(sorted(str(key).strip() for key in value if str(key).strip()))


def structured_health_evidence_from_payload(payload: object) -> StructuredHealthEvidence:
    if payload is None:
        return StructuredHealthEvidence()
    if not isinstance(payload, Mapping):
        return StructuredHealthEvidence(
            status="malformed",
            detail="Health endpoint returned a non-object structured payload.",
        )
    reported_status = _text(payload.get("status") or payload.get("state"))
    normalized_status = reported_status.casefold()
    status: StructuredHealthStatus = "unchecked"
    if normalized_status in _PASS_STATUSES:
        status = "pass"
    elif normalized_status in _FAIL_STATUSES:
        status = "fail"
    elif reported_status:
        status = "malformed"
    return StructuredHealthEvidence(
        status=status,
        reported_status=reported_status,
        version=_text(payload.get("version") or payload.get("build_version")),
        source_git_ref=_text(payload.get("source_git_ref") or payload.get("commit")),
        image_reference=_text(payload.get("image_reference") or payload.get("image")),
        detail=_text(payload.get("detail") or payload.get("summary")),
        components=_component_names(payload.get("components")),
    )
