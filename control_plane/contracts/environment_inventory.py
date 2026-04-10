from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.promotion_record import ArtifactIdentityReference, DeploymentEvidence, HealthcheckEvidence
from control_plane.contracts.promotion_record import PostDeployUpdateEvidence


class EnvironmentInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    artifact_identity: ArtifactIdentityReference | None = None
    source_git_ref: str
    deploy: DeploymentEvidence
    post_deploy_update: PostDeployUpdateEvidence = Field(default_factory=PostDeployUpdateEvidence)
    destination_health: HealthcheckEvidence = Field(default_factory=HealthcheckEvidence)
    updated_at: str
    deployment_record_id: str
    promotion_record_id: str = ""
    promoted_from_instance: str = ""

    @model_validator(mode="after")
    def _validate_inventory(self) -> "EnvironmentInventory":
        if not self.context.strip():
            raise ValueError("environment inventory requires context")
        if not self.instance.strip():
            raise ValueError("environment inventory requires instance")
        if not self.source_git_ref.strip():
            raise ValueError("environment inventory requires source_git_ref")
        if not self.updated_at.strip():
            raise ValueError("environment inventory requires updated_at")
        if not self.deployment_record_id.strip():
            raise ValueError("environment inventory requires deployment_record_id")
        if self.promotion_record_id.strip() and not self.promoted_from_instance.strip():
            raise ValueError("environment inventory with promotion_record_id requires promoted_from_instance")
        return self
