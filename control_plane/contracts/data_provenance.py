from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict


FreshnessStatus = Literal["verified", "recorded", "stale", "missing", "unsupported"]
SourceKind = Literal["record", "provider", "descriptor", "unsupported"]
AgentContextSensitivity = Literal["public", "internal", "restricted"]
AgentContextEvidenceState = FreshnessStatus

_MAX_AGENT_CONTEXT_TEXT_LENGTH = 240
_PATH_PATTERN = re.compile(r"(?<!\w)(?:/[^\s,;:=]+){2,}")
_TOKEN_ASSIGNMENT_PATTERN = re.compile(
    r"(?i)\b([a-z0-9_.-]*(?:token|secret|password|passwd|api[_-]?key|bearer)[a-z0-9_.-]*)"
    r"\s*[:=]\s*([^\s,;]+)"
)
_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}")
_LONG_SECRET_PATTERN = re.compile(r"(?<![a-zA-Z0-9])[a-zA-Z0-9._~+/=-]{32,}(?![a-zA-Z0-9])")


class DataProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: SourceKind
    source_record_id: str = ""
    recorded_at: str = ""
    refreshed_at: str = ""
    freshness_status: FreshnessStatus
    stale_after: str = ""
    detail: str = ""


class AgentContextEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    state: AgentContextEvidenceState
    detail: str
    source_url: str = ""
    sensitivity: AgentContextSensitivity = "public"
    recorded_at: str = ""


class AgentContextProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: SourceKind
    freshness_status: FreshnessStatus
    source_url: str = ""
    source_record_id: str = ""
    recorded_at: str = ""
    refreshed_at: str = ""
    stale_after: str = ""
    detail: str = ""
    sensitivity: AgentContextSensitivity = "public"


def safe_agent_context_text(value: str, *, fallback: str) -> str:
    """Return bounded text suitable for agent-facing context payloads."""

    text = " ".join(value.strip().split())
    if not text:
        return fallback
    text = _PATH_PATTERN.sub("[redacted-path]", text)
    text = _TOKEN_ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group(1)}=[redacted]", text)
    text = _BEARER_PATTERN.sub("Bearer [redacted]", text)
    text = _LONG_SECRET_PATTERN.sub("[redacted-secret]", text)
    if len(text) > _MAX_AGENT_CONTEXT_TEXT_LENGTH:
        text = text[: _MAX_AGENT_CONTEXT_TEXT_LENGTH - 1].rstrip() + "..."
    return text or fallback


def agent_safe_host_label(value: str) -> str:
    """Collapse local host identity to a non-topology-bearing label."""

    if value.strip():
        return "claimed_local_worker"
    return ""
