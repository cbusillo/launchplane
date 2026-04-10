from pydantic import BaseModel, ConfigDict, Field


class ArtifactImageReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    digest: str
    tags: tuple[str, ...] = ()


class ArtifactIdentityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    artifact_id: str
    odoo_ai_commit: str
    enterprise_base_digest: str
    image: ArtifactImageReference
