from __future__ import annotations

from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.data_provenance import (
    AgentContextEvidence,
    AgentContextProvenance,
    agent_safe_host_label,
    safe_agent_context_text,
)
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestState,
)


EveryCodeSummaryStatus = Literal["active", "stuck", "complete", "rerunnable"]


class EveryCodeSummaryStore(Protocol):
    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...


class EveryCodeWorkRequestSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str
    repository: str
    issue_number: int = Field(ge=1)
    issue_url: str
    issue_title: str = ""
    state: EveryCodeWorkRequestState
    summary_status: EveryCodeSummaryStatus
    source: str
    trigger_label: str
    trigger_actor: str = ""
    claimed_by_host: str = ""
    queued_at: str
    updated_at: str
    claimed_at: str = ""
    started_at: str = ""
    finished_at: str = ""
    result_pr_url: str = ""
    result_summary: str = ""
    safe_to_rerun: bool
    next_action: str
    provenance: AgentContextProvenance
    evidence: tuple[AgentContextEvidence, ...] = ()

    @model_validator(mode="after")
    def _validate_summary(self) -> "EveryCodeWorkRequestSummary":
        if not self.request_id.strip():
            raise ValueError("Every Code summary requires request_id")
        if not self.repository.strip() or "/" not in self.repository.strip():
            raise ValueError("Every Code summary requires owner/repo repository")
        if not self.issue_url.strip():
            raise ValueError("Every Code summary requires issue_url")
        return self


class EveryCodeSummaryReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    generated_at: str
    repository: str = ""
    issue_number: int | None = Field(default=None, ge=1)
    state_filter: EveryCodeWorkRequestState | Literal[""] = ""
    summaries: tuple[EveryCodeWorkRequestSummary, ...]

    @model_validator(mode="after")
    def _validate_read_model(self) -> "EveryCodeSummaryReadModel":
        if not self.generated_at.strip():
            raise ValueError("Every Code summary read model requires generated_at")
        return self


def build_every_code_summary_read_model(
    *,
    generated_at: str,
    record_store: EveryCodeSummaryStore,
    repository: str = "",
    issue_number: int | None = None,
    state: str = "",
    limit: int = 50,
    offset: int = 0,
) -> EveryCodeSummaryReadModel:
    normalized_repository = repository.strip()
    normalized_state = state.strip()
    if normalized_state not in {"", "queued", "claimed", "running", "done", "blocked"}:
        raise ValueError("Every Code summary state filter is invalid")
    records = record_store.list_every_code_work_request_records(
        state=normalized_state,
        repository=normalized_repository,
        limit=limit,
        offset=offset,
    )
    if issue_number is not None:
        records = tuple(record for record in records if record.issue_number == issue_number)
    return EveryCodeSummaryReadModel(
        generated_at=generated_at,
        repository=normalized_repository,
        issue_number=issue_number,
        state_filter=cast(EveryCodeWorkRequestState | Literal[""], normalized_state),
        summaries=tuple(_summary_from_record(record) for record in records),
    )


def _summary_from_record(record: EveryCodeWorkRequestRecord) -> EveryCodeWorkRequestSummary:
    summary_status = _summary_status(record)
    return EveryCodeWorkRequestSummary(
        request_id=record.request_id,
        repository=record.repository,
        issue_number=record.issue_number,
        issue_url=record.issue_url,
        issue_title=safe_agent_context_text(record.issue_title, fallback=""),
        state=record.state,
        summary_status=summary_status,
        source=record.source,
        trigger_label=record.trigger_label,
        trigger_actor=record.trigger_actor,
        claimed_by_host=agent_safe_host_label(record.claimed_by_host),
        queued_at=record.queued_at,
        updated_at=record.updated_at,
        claimed_at=record.claimed_at,
        started_at=record.started_at,
        finished_at=record.finished_at,
        result_pr_url=record.result_pr_url,
        result_summary=(
            safe_agent_context_text(record.result_summary, fallback="")
            if record.state == "done"
            else ""
        ),
        safe_to_rerun=record.state in {"done", "blocked"},
        next_action=_next_action(record=record, summary_status=summary_status),
        provenance=_provenance(record),
        evidence=_evidence(record=record, summary_status=summary_status),
    )


def _summary_status(record: EveryCodeWorkRequestRecord) -> EveryCodeSummaryStatus:
    if record.state in {"queued", "claimed", "running"}:
        return "active"
    if record.state == "blocked":
        return "stuck"
    if record.result_pr_url.strip():
        return "complete"
    return "rerunnable"


def _next_action(
    *,
    record: EveryCodeWorkRequestRecord,
    summary_status: EveryCodeSummaryStatus,
) -> str:
    if summary_status == "active":
        if record.state == "queued":
            return "Wait for a local worker to claim this request."
        return "Check the claimed local worker session before rerunning."
    if summary_status == "stuck":
        return "Inspect the linked issue and rerun when the blocker is resolved."
    if summary_status == "complete":
        return "Review the linked result PR or issue handoff."
    return "Rerun the request if the issue still needs local automation."


def _provenance(record: EveryCodeWorkRequestRecord) -> AgentContextProvenance:
    return AgentContextProvenance(
        source_kind="record",
        source_record_id=record.request_id,
        source_url=record.issue_url,
        recorded_at=record.queued_at,
        refreshed_at=record.updated_at,
        freshness_status="recorded",
        detail="Every Code work request record projected for agent status context.",
    )


def _evidence(
    *, record: EveryCodeWorkRequestRecord, summary_status: EveryCodeSummaryStatus
) -> tuple[AgentContextEvidence, ...]:
    evidence = [
        AgentContextEvidence(
            code="source_of_truth",
            state="verified",
            detail="Linked GitHub issue is the source of truth for requested work.",
            source_url=record.issue_url,
            recorded_at=record.queued_at,
        ),
        AgentContextEvidence(
            code="work_request_record",
            state="recorded",
            detail=f"Every Code work request is {summary_status}.",
            source_url=record.issue_url,
            recorded_at=record.updated_at or record.queued_at,
        ),
    ]
    if record.result_pr_url.strip():
        evidence.append(
            AgentContextEvidence(
                code="result_pr",
                state="verified",
                detail="Result pull request is linked from the work request record.",
                source_url=record.result_pr_url,
                recorded_at=record.finished_at or record.updated_at,
            )
        )
    return tuple(evidence)
