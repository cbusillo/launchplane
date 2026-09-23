from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.tenant_merge_eligibility import (
    _normalize_utc_timestamp,
)
from control_plane.tenant_admission_controller import (
    TenantAdmissionControllerRunOnceResult,
)
from control_plane.workflows.ship import utc_now_timestamp


class TenantAdmissionEvaluationReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    evaluation: TenantAdmissionControllerRunOnceResult
    human_actions: tuple[()] = ()
    agent_authoring_allowed: Literal[False] = False
    generated_at: str

    @model_validator(mode="after")
    def _validate_read_model(self) -> "TenantAdmissionEvaluationReadModel":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant admission evaluation schema version.")
        self.generated_at = _normalize_utc_timestamp(self.generated_at, "generated_at")
        return self


def build_tenant_admission_evaluation_read_model(
    *, evaluation: TenantAdmissionControllerRunOnceResult, generated_at: str = ""
) -> TenantAdmissionEvaluationReadModel:
    return TenantAdmissionEvaluationReadModel(
        evaluation=evaluation, generated_at=generated_at or utc_now_timestamp()
    )
