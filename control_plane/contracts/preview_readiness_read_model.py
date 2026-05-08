from __future__ import annotations

from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.every_code_preview_gate_record import (
    EveryCodePreviewGateRecord,
    EveryCodePreviewGateStatus,
)


PreviewReadinessStatus = Literal[
    "waiting_on_checks",
    "ready",
    "needs_attention",
    "cancelled",
]
PreviewReadinessFreshness = Literal["verified", "recorded", "stale", "missing", "unsupported"]


class PreviewReadinessStore(Protocol):
    def list_every_code_preview_gate_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePreviewGateRecord, ...]: ...


class PreviewReadinessItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate_id: str
    request_id: str
    repository: str
    issue_number: int = Field(ge=1)
    issue_url: str
    pr_number: int = Field(ge=1)
    pr_url: str
    head_sha: str
    gate_status: EveryCodePreviewGateStatus
    readiness_status: PreviewReadinessStatus
    freshness_status: PreviewReadinessFreshness
    provenance: str = "every_code_preview_gate"
    updated_at: str
    last_checked_at: str = ""
    ready_at: str = ""
    terminal_at: str = ""
    detail: str
    check_summary: str = ""
    source_of_truth_url: str
    safe_to_request_preview: bool
    needs_operator_attention: bool

    @model_validator(mode="after")
    def _validate_item(self) -> "PreviewReadinessItem":
        if not self.gate_id.strip():
            raise ValueError("preview readiness item requires gate_id")
        if not self.repository.strip() or "/" not in self.repository.strip():
            raise ValueError("preview readiness item requires owner/repo repository")
        if not self.issue_url.strip():
            raise ValueError("preview readiness item requires issue_url")
        if not self.pr_url.strip():
            raise ValueError("preview readiness item requires pr_url")
        if not self.source_of_truth_url.strip():
            raise ValueError("preview readiness item requires source_of_truth_url")
        if not self.updated_at.strip():
            raise ValueError("preview readiness item requires updated_at")
        return self


class PreviewReadinessReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    generated_at: str
    repository: str = ""
    pr_number: int | None = Field(default=None, ge=1)
    status_filter: EveryCodePreviewGateStatus | Literal[""] = ""
    items: tuple[PreviewReadinessItem, ...]

    @model_validator(mode="after")
    def _validate_read_model(self) -> "PreviewReadinessReadModel":
        if not self.generated_at.strip():
            raise ValueError("preview readiness read model requires generated_at")
        return self


def build_preview_readiness_read_model(
    *,
    generated_at: str,
    record_store: PreviewReadinessStore,
    repository: str = "",
    pr_number: int | None = None,
    status: str = "",
    limit: int = 50,
    offset: int = 0,
) -> PreviewReadinessReadModel:
    normalized_repository = repository.strip()
    normalized_status = status.strip()
    if normalized_status not in {"", "pending", "ready", "blocked", "labeled", "cancelled"}:
        raise ValueError("preview readiness status filter is invalid")
    gate_records = record_store.list_every_code_preview_gate_records(
        repository=normalized_repository,
        pr_number=pr_number,
        status=normalized_status,
        limit=limit,
        offset=offset,
    )
    return PreviewReadinessReadModel(
        generated_at=generated_at,
        repository=normalized_repository,
        pr_number=pr_number,
        status_filter=cast(EveryCodePreviewGateStatus | Literal[""], normalized_status),
        items=tuple(_readiness_item_from_gate(gate) for gate in gate_records),
    )


def _readiness_item_from_gate(gate: EveryCodePreviewGateRecord) -> PreviewReadinessItem:
    readiness_status = _readiness_status(gate)
    return PreviewReadinessItem(
        gate_id=gate.gate_id,
        request_id=gate.request_id,
        repository=gate.repository,
        issue_number=gate.issue_number,
        issue_url=gate.issue_url,
        pr_number=gate.pr_number,
        pr_url=gate.pr_url,
        head_sha=gate.head_sha,
        gate_status=gate.status,
        readiness_status=readiness_status,
        freshness_status=_freshness_status(gate),
        updated_at=gate.updated_at,
        last_checked_at=gate.last_checked_at,
        ready_at=gate.ready_at or gate.labeled_at,
        terminal_at=gate.cancelled_at,
        detail=_detail(gate=gate, readiness_status=readiness_status),
        check_summary=gate.check_summary,
        source_of_truth_url=gate.pr_url,
        safe_to_request_preview=gate.status in {"ready", "labeled"},
        needs_operator_attention=gate.status == "blocked",
    )


def _readiness_status(gate: EveryCodePreviewGateRecord) -> PreviewReadinessStatus:
    if gate.status == "pending":
        return "waiting_on_checks"
    if gate.status in {"ready", "labeled"}:
        return "ready"
    if gate.status == "blocked":
        return "needs_attention"
    return "cancelled"


def _freshness_status(gate: EveryCodePreviewGateRecord) -> PreviewReadinessFreshness:
    if gate.status in {"ready", "labeled", "blocked", "cancelled"}:
        return "verified"
    if gate.last_checked_at.strip():
        return "recorded"
    return "missing"


def _detail(
    *,
    gate: EveryCodePreviewGateRecord,
    readiness_status: PreviewReadinessStatus,
) -> str:
    if readiness_status == "waiting_on_checks":
        return gate.pending_reason or "Preview readiness is waiting on required checks."
    if readiness_status == "ready":
        return "Preview can be requested from the linked pull request."
    if readiness_status == "needs_attention":
        return gate.blocked_reason or "Preview readiness needs operator attention."
    return gate.blocked_reason or "Preview readiness is terminal for this pull request."
