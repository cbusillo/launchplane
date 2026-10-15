from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


OrdinaryAgentAction = Literal["self_read", "preflight", "guarded_merge"]


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OrdinaryAgentTarget(StrictFrozenModel):
    repository_id: int = Field(gt=0, le=2**63 - 1)
    repository: str = Field(max_length=140, pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_branch: str = Field(min_length=1, max_length=255)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        if any(component in {".", ".."} for component in value.split("/")):
            raise ValueError("repository owner and name must be canonical exact components")
        return value

    @field_validator("base_branch")
    @classmethod
    def validate_base_branch(cls, value: str) -> str:
        invalid_fragments = ("..", "@{", "\\", ":", "//")
        if (
            value != value.strip()
            or value == "@"
            or value.startswith(("/", ".", "-"))
            or value.endswith(("/", ".", ".lock"))
            or any(fragment in value for fragment in invalid_fragments)
            or any(
                character.isspace() or ord(character) < 32 or ord(character) == 127
                for character in value
            )
            or any(character in value for character in "*?[]~^")
            or any(
                component.startswith(".") or component.endswith(".lock")
                for component in value.split("/")
            )
        ):
            raise ValueError("base branch must be one exact canonical Git ref name")
        return value


class OrdinaryAgentPolicyRule(StrictFrozenModel):
    managed_set_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    managed_rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    target: OrdinaryAgentTarget
    actions: tuple[OrdinaryAgentAction, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_actions(self) -> OrdinaryAgentPolicyRule:
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("policy rule actions must be unique")
        return self


class OrdinaryAgentBudget(StrictFrozenModel):
    window_start: int = Field(ge=0, le=2**63 - 1)
    window_end: int = Field(ge=0, le=2**63 - 1)
    action_limit: int = Field(ge=0, le=2**63 - 1)
    actions_used: int = Field(ge=0, le=2**63 - 1)
    pull_request_limit: int = Field(ge=0, le=2**63 - 1)
    pull_requests_used: int = Field(ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def validate_window(self) -> OrdinaryAgentBudget:
        if self.window_end <= self.window_start:
            raise ValueError("budget window end must be after its start")
        return self


class OrdinaryAgentPullRequest(StrictFrozenModel):
    number: int = Field(gt=0, le=2**63 - 1)
    head_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
