"""Original-writer evidence stored separately from change-impact policy digests."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


ChangeImpactAttributionStatus = Literal[
    "attributed", "legacy_unattributed", "attribution_unavailable", "not_applied"
]


class ChangeImpactPolicyWorkflowIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(min_length=1, max_length=256)
    repository_id: str = Field(default="", max_length=64)
    repository_owner_id: str = Field(default="", max_length=64)
    workflow_ref: str = Field(max_length=512)
    job_workflow_ref: str = Field(max_length=512)
    ref: str = Field(max_length=512)
    sha: str = Field(min_length=1, max_length=64)


class ChangeImpactPolicyAuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    record_id: str = Field(min_length=1, max_length=256)
    policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    actor_kind: Literal["local_admin", "local_operator", "github_actions"]
    actor_subject: str = Field(min_length=1, max_length=512)
    workflow_identity: ChangeImpactPolicyWorkflowIdentity | None = None
    trace_id: str = Field(min_length=1, max_length=256)
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def _validate_actor(self) -> "ChangeImpactPolicyAuditRecord":
        if (self.actor_kind == "github_actions") != (self.workflow_identity is not None):
            raise ValueError("Workflow identity must describe exactly GitHub Actions actors.")
        if not self.actor_subject.strip():
            raise ValueError("Policy audit actor subject must not be blank.")
        return self


class ChangeImpactPolicyAuditedWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["written", "replayed"]
    audit: ChangeImpactPolicyAuditRecord | None = None
