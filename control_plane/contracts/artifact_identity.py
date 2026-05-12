from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ArtifactBaseImageRole = Literal["runtime", "devtools"]


class ArtifactAddonSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    ref: str


class ArtifactAddonSelector(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    selector: str
    resolved_ref: str


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

    @field_validator("repository", "digest", mode="after")
    @classmethod
    def _validate_required_string(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("artifact image reference requires repository and digest")
        return value.strip()


class ArtifactBaseImageProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: ArtifactBaseImageRole
    image: ArtifactImageReference
    source_repository: str = ""
    source_ref: str = ""

    @model_validator(mode="after")
    def _validate_source_pair(self) -> "ArtifactBaseImageProvenance":
        self.source_repository = self.source_repository.strip()
        self.source_ref = self.source_ref.strip()
        if bool(self.source_repository) != bool(self.source_ref):
            raise ValueError(
                "artifact base image provenance requires source_repository and source_ref together"
            )
        return self


class ArtifactBuildToolProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str = ""
    source_repository: str = ""
    source_ref: str = ""

    @model_validator(mode="after")
    def _validate_build_tool(self) -> "ArtifactBuildToolProvenance":
        self.name = self.name.strip()
        self.version = self.version.strip()
        self.source_repository = self.source_repository.strip()
        self.source_ref = self.source_ref.strip()
        if not self.name:
            raise ValueError("artifact build tool provenance requires name")
        if bool(self.source_repository) != bool(self.source_ref):
            raise ValueError(
                "artifact build tool provenance requires source_repository and source_ref together"
            )
        if not self.version and not self.source_ref:
            raise ValueError(
                "artifact build tool provenance requires version or source_repository/source_ref"
            )
        return self


class ArtifactBuildProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_images: tuple[ArtifactBaseImageProvenance, ...] = ()
    build_tools: tuple[ArtifactBuildToolProvenance, ...] = ()

    @model_validator(mode="after")
    def _validate_build_provenance(self) -> "ArtifactBuildProvenance":
        roles: set[ArtifactBaseImageRole] = set()
        for base_image in self.base_images:
            if base_image.role in roles:
                raise ValueError(
                    f"artifact build provenance cannot contain duplicate base image role {base_image.role!r}"
                )
            roles.add(base_image.role)
        return self


class ArtifactIdentityManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    artifact_id: str
    source_commit: str
    enterprise_base_digest: str
    addon_sources: tuple[ArtifactAddonSource, ...] = ()
    addon_selectors: tuple[ArtifactAddonSelector, ...] = ()
    openupgrade_inputs: ArtifactOpenUpgradeInputs = Field(default_factory=ArtifactOpenUpgradeInputs)
    build_flags: ArtifactBuildFlags = Field(default_factory=ArtifactBuildFlags)
    build_provenance: ArtifactBuildProvenance = Field(default_factory=ArtifactBuildProvenance)
    image: ArtifactImageReference
