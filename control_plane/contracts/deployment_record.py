from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
)
from control_plane.contracts.ship_request import BranchSyncEvidence

DelegatedExecutor = Literal[
    "control-plane.dokploy",
]


class ResolvedTargetEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_type: Literal["compose", "application"]
    target_id: str
    target_name: str

    @model_validator(mode="after")
    def _validate_target(self) -> "ResolvedTargetEvidence":
        if not self.target_id.strip():
            raise ValueError("resolved target evidence requires target_id")
        if not self.target_name.strip():
            raise ValueError("resolved target evidence requires target_name")
        return self


class DeploymentRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    artifact_identity: ArtifactIdentityReference | None = None
    context: str
    instance: str
    source_git_ref: str
    wait_for_completion: bool = True
    verify_destination_health: bool = True
    no_cache: bool = False
    delegated_executor: DelegatedExecutor = "control-plane.dokploy"
    branch_sync: BranchSyncEvidence | None = None
    resolved_target: ResolvedTargetEvidence | None = None
    deploy: DeploymentEvidence
    post_deploy_update: PostDeployUpdateEvidence = Field(default_factory=PostDeployUpdateEvidence)
    destination_health: HealthcheckEvidence = Field(default_factory=HealthcheckEvidence)

    @model_validator(mode="after")
    def _validate_record(self) -> "DeploymentRecord":
        if not self.context.strip():
            raise ValueError("deployment record requires context")
        if not self.instance.strip():
            raise ValueError("deployment record requires instance")
        if not self.source_git_ref.strip():
            raise ValueError("deployment record requires source_git_ref")
        return self
