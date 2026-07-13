from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from control_plane.child_process_errors import redact_untrusted_text


FreshnessStatus = Literal["verified", "recorded", "stale", "missing", "unsupported"]
SourceKind = Literal["record", "provider", "descriptor", "unsupported"]
AgentContextSensitivity = Literal["public", "internal", "restricted"]
AgentContextEvidenceState = FreshnessStatus


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

    return redact_untrusted_text(value, fallback=fallback)


def agent_safe_host_label(value: str) -> str:
    """Collapse local host identity to a non-topology-bearing label."""

    if value.strip():
        return "claimed_local_worker"
    return ""
