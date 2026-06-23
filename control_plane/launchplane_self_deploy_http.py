from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.workflows.launchplane_self_deploy import (
    LaunchplaneSelfDeployRequest,
    LaunchplaneSelfDeployResult,
)


LAUNCHPLANE_SELF_DEPLOY_ROUTE = "/v1/drivers/launchplane/self-deploy"


class LaunchplaneSelfDeployEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    deploy: LaunchplaneSelfDeployRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "LaunchplaneSelfDeployEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Launchplane self deploy requires product 'launchplane'.")
        self.product = "launchplane"
        return self


def launchplane_self_deploy_records(
    result: LaunchplaneSelfDeployResult,
) -> dict[str, str]:
    result_payload = result.model_dump(mode="json")
    return {
        "target_type": str(result_payload["target_type"]),
        "target_id": str(result_payload["target_id"]),
        "image_reference": str(result_payload["image_reference"]),
        "oauth_env_keys_removed": str(result_payload["oauth_env_keys_removed"]),
    }
