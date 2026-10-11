from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.authz_access_read import AuthzPolicyRecordSummary

AUTHZ_RETIREMENT_MAX_CANDIDATES = 25
AUTHZ_RETIREMENT_MAX_RULES = 2000


class AuthzWorkflowEvidenceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=512)
    workflow_path: str = Field(
        pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml$",
        max_length=1024,
    )
    binding: Literal["workflow_ref", "job_workflow_ref"] = "workflow_ref"


class AuthzRetirementEvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_record_id: str = Field(min_length=1, max_length=256)
    policy_revision: int = Field(ge=1)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    managed_set_ids: tuple[str, ...] = Field(default=(), max_length=AUTHZ_RETIREMENT_MAX_CANDIDATES)
    workflows: tuple[AuthzWorkflowEvidenceCandidate, ...] = Field(
        default=(), max_length=AUTHZ_RETIREMENT_MAX_CANDIDATES
    )

    @model_validator(mode="after")
    def _validate_scope(self) -> AuthzRetirementEvidenceRequest:
        if not self.managed_set_ids and not self.workflows:
            raise ValueError("Retirement evidence requires an explicit comparison scope.")
        if any(
            not re.fullmatch(r"[a-z0-9][a-z0-9._:/-]{0,159}", value)
            for value in self.managed_set_ids
        ):
            raise ValueError("Managed set comparisons require exact stable IDs.")
        if len(set(self.managed_set_ids)) != len(self.managed_set_ids):
            raise ValueError("Managed set comparisons must be unique.")
        if len(set(self.workflows)) != len(self.workflows):
            raise ValueError("Workflow comparisons must be unique.")
        return self


class AuthzSetMaintenanceObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int
    policy_sha256: str
    caller_type: Literal[
        "github_actions",
        "github_human",
        "terminal_agent",
        "local_operator",
        "local_admin",
        "unknown",
    ]
    introduced: bool


class AuthzManagedSetEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int
    presence: Literal["present", "absent", "unknown"]
    rule_count: int | None = None
    import_origin: Literal["unknown"] = "unknown"
    independent_maintenance: Literal["unknown"] = "unknown"
    reason_codes: tuple[str, ...]
    observations: tuple[AuthzSetMaintenanceObservation, ...] = ()


class AuthzUnmanagedWorkflowEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int
    state: Literal["potential_dependency", "no_matching_rule", "unknown"]
    rule_sha256s: tuple[str, ...] = ()
    reason_codes: tuple[str, ...] = ()


class AuthzRetirementHistoryCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    returned_count: int
    oldest_revision: int | None = None
    state: Literal["complete", "partial"]
    reason_codes: tuple[str, ...]


class AuthzRetirementEvidenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    trace_id: str
    policy: AuthzPolicyRecordSummary
    history: AuthzRetirementHistoryCoverage
    managed_sets: tuple[AuthzManagedSetEvidence, ...]
    unmanaged_workflows: tuple[AuthzUnmanagedWorkflowEvidence, ...]
