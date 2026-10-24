from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class WorkflowRuleSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rule_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    managed_set_id: str | None = Field(default=None, max_length=256)
    managed_rule_id: str | None = Field(default=None, max_length=256)


class WorkflowRuleRetirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_record_id: str = Field(min_length=1, max_length=256)
    policy_revision: int = Field(ge=1)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection: tuple[WorkflowRuleSelection, ...] = Field(min_length=1, max_length=50)
    mode: Literal["inspect", "plan"] = "inspect"

    @model_validator(mode="after")
    def validate_unique(self) -> "WorkflowRuleRetirementRequest":
        if len({selection.rule_sha256 for selection in self.selection}) != len(self.selection):
            raise ValueError("Select unique workflow-rule fingerprints.")
        return self
