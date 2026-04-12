from pydantic import BaseModel, ConfigDict, Field


class ArtifactAddonSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    ref: str


class ArtifactOpenUpgradeInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addon_repository: str = ""
    install_spec: str = ""


class ArtifactBuildFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")

    addon_skip_flags: tuple[str, ...] = ()
    values: dict[str, str] = Field(default_factory=dict)


class ArtifactImageReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    digest: str
    tags: tuple[str, ...] = ()


class ArtifactIdentityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    artifact_id: str
    source_commit: str
    enterprise_base_digest: str
    addon_sources: tuple[ArtifactAddonSource, ...] = ()
    openupgrade_inputs: ArtifactOpenUpgradeInputs = Field(default_factory=ArtifactOpenUpgradeInputs)
    build_flags: ArtifactBuildFlags = Field(default_factory=ArtifactBuildFlags)
    image: ArtifactImageReference
