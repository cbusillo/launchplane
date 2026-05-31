from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deploy_target import DeployedTargetReference
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    BootstrapEvidence,
    DeploymentEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity

DelegatedExecutor = Literal["control-plane.dokploy"]


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

    def to_deployed_target_reference(self) -> DeployedTargetReference:
        return DeployedTargetReference(
            provider_id="dokploy",
            target_category=self.target_type,
            target_id=self.target_id,
            display_name=self.target_name,
            provider_target_type=self.target_type,
        )


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
    resolved_target: ResolvedTargetEvidence | None = None
    deployed_target: DeployedTargetReference | None = None
    runtime_source: dict[str, str] = Field(default_factory=dict)
    runtime_identity: RuntimeIdentity | None = None
    deploy: DeploymentEvidence
    bootstrap: BootstrapEvidence = Field(default_factory=BootstrapEvidence)
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
        if self.deployed_target is None and self.resolved_target is not None:
            self.deployed_target = self.resolved_target.to_deployed_target_reference()
        return self
