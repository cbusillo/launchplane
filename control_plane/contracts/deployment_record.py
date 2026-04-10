from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
)

DelegatedExecutor = Literal["odoo-ai.compatibility-ship-worker"]


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
    delegated_executor: DelegatedExecutor = "odoo-ai.compatibility-ship-worker"
    deploy: DeploymentEvidence
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
