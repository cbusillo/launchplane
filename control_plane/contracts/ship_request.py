from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deploy_target import (
    DeployTargetCategory,
    DeployTargetContractReference,
    apply_target_reference_defaults,
    ensure_target_reference_matches,
)
from control_plane.contracts.dokploy_target_record import DokployTargetType
from control_plane.contracts.promotion_record import HealthcheckEvidence


class ShipRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    artifact_id: str
    context: str
    instance: str
    source_git_ref: str
    target_name: str
    target_type: DokployTargetType
    target_reference: DeployTargetContractReference | None = Field(
        default=None, exclude=True
    )
    provider_id: str = "dokploy"
    target_category: DeployTargetCategory | None = None
    provider_target_type: str = ""
    deploy_mode: str
    provider_deploy_mode: str = ""
    wait: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)
    verify_health: bool = True
    health_timeout_seconds: int | None = Field(default=None, ge=1)
    dry_run: bool = False
    no_cache: bool = False
    allow_dirty: bool = False
    destination_health: HealthcheckEvidence = Field(default_factory=HealthcheckEvidence)

    @model_validator(mode="before")
    @classmethod
    def _apply_target_reference_defaults(cls, data: object) -> object:
        return apply_target_reference_defaults(data)

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
        self.provider_id = self.provider_id.strip().lower()
        self.provider_target_type = self.provider_target_type.strip().lower()
        self.provider_deploy_mode = self.provider_deploy_mode.strip()
        if not self.provider_id:
            raise ValueError("ship request requires provider_id")
        if self.target_category is None:
            self.target_category = self.target_type
        if not self.provider_target_type:
            self.provider_target_type = self.target_type
        if not self.provider_deploy_mode:
            self.provider_deploy_mode = self.deploy_mode
        self.target_reference = ensure_target_reference_matches(
            self.target_reference,
            target_name=self.target_name,
            target_type=self.target_type,
            provider_id=self.provider_id,
            target_category=self.target_category,
            provider_target_type=self.provider_target_type,
        )
        return self
