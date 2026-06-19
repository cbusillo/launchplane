from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    build_every_code_work_request_id,
    build_every_code_work_request_lifecycle_id,
)


class EveryCodeWorkRequestCreateEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    issue_number: int = Field(ge=1)
    issue_url: str
    issue_title: str = ""
    trigger_label: str = "every-code"
    trigger_actor: str = ""
    github_delivery_id: str = ""
    source: Literal["github_issue_label", "manual", "reconciliation"] = "manual"
    queued_at: str = ""


def build_every_code_work_request_record(
    request: EveryCodeWorkRequestCreateEnvelope, *, queued_at: str
) -> EveryCodeWorkRequestRecord:
    request_id = build_every_code_work_request_id(
        repository=request.repository,
        issue_number=request.issue_number,
        trigger_label=request.trigger_label,
    )
    return EveryCodeWorkRequestRecord(
        request_id=request_id,
        lifecycle_id=build_every_code_work_request_lifecycle_id(
            request_id=request_id,
            queued_at=queued_at,
        ),
        source=request.source,
        state="queued",
        repository=request.repository.strip(),
        issue_number=request.issue_number,
        issue_url=request.issue_url.strip(),
        issue_title=request.issue_title.strip(),
        trigger_label=request.trigger_label.strip(),
        trigger_actor=request.trigger_actor.strip(),
        github_delivery_id=request.github_delivery_id.strip(),
        queued_at=queued_at,
        updated_at=queued_at,
    )
