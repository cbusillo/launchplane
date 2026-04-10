from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.promotion_record import HealthcheckEvidence


class ShipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    artifact_id: str
    context: str
    instance: str
    source_git_ref: str
    target_name: str
    target_type: str
    deploy_mode: str
    wait: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)
    verify_health: bool = True
    health_timeout_seconds: int | None = Field(default=None, ge=1)
    dry_run: bool = False
    no_cache: bool = False
    allow_dirty: bool = False
    destination_health: HealthcheckEvidence = Field(default_factory=HealthcheckEvidence)

    @model_validator(mode="after")
    def _validate_request(self) -> "ShipRequest":
        if not self.artifact_id.strip():
            raise ValueError("ship request requires artifact_id")
        if not self.context.strip():
            raise ValueError("ship request requires context")
        if not self.instance.strip():
            raise ValueError("ship request requires instance")
        if not self.source_git_ref.strip():
            raise ValueError("ship request requires source_git_ref")
        return self
