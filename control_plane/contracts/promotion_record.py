from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReleaseStatus = Literal["pending", "pass", "fail", "skipped"]


class ArtifactIdentityReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    manifest_version: int = Field(default=1, ge=1)


class HealthcheckEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verified: bool = False
    urls: tuple[str, ...] = ()
    timeout_seconds: int | None = Field(default=None, ge=1)
    status: ReleaseStatus = "skipped"

    @model_validator(mode="after")
    def _validate_verified_healthcheck(self) -> "HealthcheckEvidence":
        if not self.verified:
            return self
        if not self.urls:
            raise ValueError("verified healthcheck evidence requires at least one URL")
        if self.timeout_seconds is None:
            raise ValueError("verified healthcheck evidence requires timeout_seconds")
        if self.status not in {"pass", "fail"}:
            raise ValueError("verified healthcheck evidence requires pass/fail status")
        return self


class BackupGateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = True
    status: ReleaseStatus = "pending"
    evidence: dict[str, str] = Field(default_factory=dict)


class DeploymentEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_name: str
    target_type: Literal["compose", "application"]
    deploy_mode: str
    deployment_id: str = ""
    status: ReleaseStatus = "pending"
    started_at: str = ""
    finished_at: str = ""


class PostDeployUpdateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempted: bool = False
    status: ReleaseStatus = "skipped"
    detail: str = ""

    @model_validator(mode="after")
    def _validate_attempted_update(self) -> "PostDeployUpdateEvidence":
        if not self.attempted and self.status != "skipped":
            raise ValueError("non-attempted post-deploy update must use skipped status")
        if self.attempted and self.status not in {"pending", "pass", "fail"}:
            raise ValueError("attempted post-deploy update must use pending/pass/fail status")
        return self


class PromotionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    artifact_identity: ArtifactIdentityReference
    backup_record_id: str = ""
    context: str
    from_instance: str
    to_instance: str
    source_health: HealthcheckEvidence = Field(default_factory=HealthcheckEvidence)
    backup_gate: BackupGateEvidence = Field(default_factory=BackupGateEvidence)
    deploy: DeploymentEvidence
    post_deploy_update: PostDeployUpdateEvidence = Field(default_factory=PostDeployUpdateEvidence)
    destination_health: HealthcheckEvidence = Field(default_factory=HealthcheckEvidence)

    @model_validator(mode="after")
    def _validate_promotion_path(self) -> "PromotionRecord":
        if self.from_instance == self.to_instance:
            raise ValueError("promotion source and destination instances must differ")
        if self.backup_gate.required and self.backup_gate.status == "pass" and not self.backup_record_id.strip():
            raise ValueError("promotion record with passing backup gate requires backup_record_id")
        return self


class PromotionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    artifact_id: str
    backup_record_id: str = ""
    source_git_ref: str
    context: str
    from_instance: str
    to_instance: str
    target_name: str
    target_type: Literal["compose", "application"]
    deploy_mode: str
    wait: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)
    verify_health: bool = True
    health_timeout_seconds: int | None = Field(default=None, ge=1)
    dry_run: bool = False
    no_cache: bool = False
    allow_dirty: bool = False
    source_health: HealthcheckEvidence = Field(default_factory=HealthcheckEvidence)
    backup_gate: BackupGateEvidence = Field(default_factory=BackupGateEvidence)
    destination_health: HealthcheckEvidence = Field(default_factory=HealthcheckEvidence)

    @model_validator(mode="after")
    def _validate_request(self) -> "PromotionRequest":
        if not self.artifact_id.strip():
            raise ValueError("promotion request requires artifact_id")
        if not self.source_git_ref.strip():
            raise ValueError("promotion request requires source_git_ref")
        if not self.context.strip():
            raise ValueError("promotion request requires context")
        if not self.target_name.strip():
            raise ValueError("promotion request requires target_name")
        if not self.deploy_mode.strip():
            raise ValueError("promotion request requires deploy_mode")
        if self.from_instance == self.to_instance:
            raise ValueError("promotion source and destination instances must differ")
        return self
