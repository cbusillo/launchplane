from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationAuditRecord


class RunnerLaneRegistrationAuditEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    audit: RunnerLaneRegistrationAuditRecord

    @model_validator(mode="after")
    def _validate_alignment(self) -> "RunnerLaneRegistrationAuditEvidenceEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError(
                "runner lane registration audit evidence requires product 'launchplane'"
            )
        self.product = "launchplane"
        return self
