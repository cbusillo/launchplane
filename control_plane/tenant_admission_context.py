from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.tenant_merge_eligibility import (
    TenantAdmissionPathResult,
    TenantAdmissionPathState,
    _normalize_utc_timestamp,
)
from control_plane.tenant_admission_controller import (
    TenantAdmissionControllerRunOnceResult,
)
from control_plane.workflows.ship import utc_now_timestamp


TenantAdmissionHumanActionKind = Literal[
    "manager_preview_approval",
    "technical_human_waiver",
]
TenantAdmissionHumanActionAvailability = Literal[
    "available",
    "satisfied",
    "unavailable",
]


class TenantAdmissionHumanActionReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    action_kind: TenantAdmissionHumanActionKind
    availability: TenantAdmissionHumanActionAvailability
    path_state: TenantAdmissionPathState
    title: str
    detail: str
    requires_human: Literal[True] = True
    agent_authoring_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _validate_action(self) -> "TenantAdmissionHumanActionReadModel":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant admission human action schema version.")
        self.title = self.title.strip()
        self.detail = self.detail.strip()
        if not self.title or not self.detail:
            raise ValueError("Tenant admission human action requires title and detail.")
        expected_availability: TenantAdmissionHumanActionAvailability
        if self.path_state == "satisfied":
            expected_availability = "satisfied"
        elif self.path_state == "unavailable":
            expected_availability = "unavailable"
        else:
            expected_availability = "available"
        if self.availability != expected_availability:
            raise ValueError(
                "Tenant admission human action availability does not match path state."
            )
        return self


class TenantAdmissionEvaluationReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    evaluation: TenantAdmissionControllerRunOnceResult
    human_actions: tuple[TenantAdmissionHumanActionReadModel, ...] = ()
    agent_authoring_allowed: Literal[False] = False
    generated_at: str

    @model_validator(mode="after")
    def _validate_read_model(self) -> "TenantAdmissionEvaluationReadModel":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant admission evaluation schema version.")
        self.generated_at = _normalize_utc_timestamp(self.generated_at, "generated_at")
        action_kinds = tuple(action.action_kind for action in self.human_actions)
        if len(action_kinds) != len(set(action_kinds)):
            raise ValueError("Tenant admission evaluation human actions must be unique.")
        admission = self.evaluation.admission
        if admission is None or admission.classification_kind != "tenant_ui":
            if self.human_actions:
                raise ValueError("Only tenant UI admission evaluations can expose human actions.")
        elif set(action_kinds) != {
            "manager_preview_approval",
            "technical_human_waiver",
        }:
            raise ValueError(
                "Tenant UI admission evaluation requires manager and owner human actions."
            )
        return self


def build_tenant_admission_evaluation_read_model(
    *,
    evaluation: TenantAdmissionControllerRunOnceResult,
    generated_at: str = "",
) -> TenantAdmissionEvaluationReadModel:
    admission = evaluation.admission
    human_actions: tuple[TenantAdmissionHumanActionReadModel, ...] = ()
    if admission is not None and admission.classification_kind == "tenant_ui":
        human_actions = (
            _human_action(
                action_kind="manager_preview_approval",
                path=admission.paths.manager_preview_approval,
            ),
            _human_action(
                action_kind="technical_human_waiver",
                path=admission.paths.technical_human_waiver,
            ),
        )
    return TenantAdmissionEvaluationReadModel(
        evaluation=evaluation,
        human_actions=human_actions,
        generated_at=generated_at or utc_now_timestamp(),
    )


def _human_action(
    *,
    action_kind: TenantAdmissionHumanActionKind,
    path: TenantAdmissionPathResult | None,
) -> TenantAdmissionHumanActionReadModel:
    path_state: TenantAdmissionPathState = path.state if path is not None else "unavailable"
    availability: TenantAdmissionHumanActionAvailability
    if path_state == "satisfied":
        availability = "satisfied"
    elif path_state == "unavailable":
        availability = "unavailable"
    else:
        availability = "available"
    if action_kind == "manager_preview_approval":
        title = "Manager preview approval"
        details = {
            "satisfied": "The manager approval is current for this exact preview and head.",
            "pending": "The manager can review the current preview and approve its exact fingerprint.",
            "stale": "The prior manager approval is stale; review and approve the current preview fingerprint.",
            "denied": "Manager feedback must be resolved before the current preview can be approved.",
            "unavailable": "A current manager preview approval path is unavailable for this exact head.",
        }
    else:
        title = "Repository-owner technical waiver"
        details = {
            "satisfied": "The repository-owner waiver is current for this exact head and authority.",
            "pending": "An authorized repository owner can issue a reasoned waiver for this exact head.",
            "stale": "The prior waiver is stale; an authorized repository owner can review the current head.",
            "denied": "The prior waiver is denied or revoked; an authorized owner may issue a new exact-head waiver.",
            "unavailable": "Current repository-owner waiver authority is unavailable for this exact head.",
        }
    return TenantAdmissionHumanActionReadModel(
        action_kind=action_kind,
        availability=availability,
        path_state=path_state,
        title=title,
        detail=details[path_state],
    )
