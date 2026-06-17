from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord


class RunnerHostHygieneAuditEvidenceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    audit: RunnerHostHygieneApplyAuditRecord

    @model_validator(mode="after")
    def _validate_alignment(self) -> "RunnerHostHygieneAuditEvidenceEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("runner host hygiene audit evidence requires product 'launchplane'")
        self.product = "launchplane"
        return self
