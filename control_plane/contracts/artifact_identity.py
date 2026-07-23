from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.artifact_dependency_provenance import (
    ArtifactDependencyProvenance,
    normalize_artifact_git_commit,
    normalize_artifact_repository_identity,
    normalize_artifact_sha256_digest,
)

ArtifactBaseImageRole = Literal["runtime", "devtools"]


def _artifact_manifest_json_schema(schema: dict[str, Any]) -> None:
    dependency_schema = schema["properties"]["dependency_provenance"]["anyOf"][0]
    schema["oneOf"] = [
        {
            "properties": {
                "schema_version": {"const": 1},
                "dependency_provenance": {"type": "null"},
            }
        },
        {
            "properties": {
                "schema_version": {"const": 2},
                "dependency_provenance": dependency_schema,
            },
            "required": ["schema_version", "dependency_provenance"],
        },
    ]


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
    model_config = ConfigDict(extra="forbid", json_schema_extra=_artifact_manifest_json_schema)

    schema_version: Literal[1, 2] = 1
    artifact_id: str
    source_commit: str
    enterprise_base_digest: str
    addon_sources: tuple[ArtifactAddonSource, ...] = ()
    addon_selectors: tuple[ArtifactAddonSelector, ...] = ()
    odoo_install_modules: tuple[str, ...] = ()
    openupgrade_inputs: ArtifactOpenUpgradeInputs = Field(default_factory=ArtifactOpenUpgradeInputs)
    build_flags: ArtifactBuildFlags = Field(default_factory=ArtifactBuildFlags)
    build_provenance: ArtifactBuildProvenance = Field(default_factory=ArtifactBuildProvenance)
    dependency_provenance: ArtifactDependencyProvenance | None = None
    image: ArtifactImageReference

    @model_validator(mode="after")
    def _validate_manifest_version(self) -> "ArtifactIdentityManifest":
        if self.schema_version == 1:
            if self.dependency_provenance is not None:
                raise ValueError(
                    "artifact manifest dependency_provenance requires schema_version 2"
                )
            return self

        dependency_provenance = self.dependency_provenance
        if dependency_provenance is None:
            raise ValueError("artifact manifest schema_version 2 requires dependency_provenance")

        self.source_commit = normalize_artifact_git_commit(
            self.source_commit,
            label="artifact manifest source_commit",
        )
        self.enterprise_base_digest = normalize_artifact_sha256_digest(
            self.enterprise_base_digest,
            label="artifact manifest enterprise_base_digest",
        )
        self.image.digest = normalize_artifact_sha256_digest(
            self.image.digest,
            label="artifact manifest image digest",
        )

        base_image_roles = {base_image.role for base_image in self.build_provenance.base_images}
        if base_image_roles != {"runtime", "devtools"}:
            raise ValueError(
                "artifact manifest schema_version 2 requires runtime and devtools base image provenance"
            )
        for base_image in self.build_provenance.base_images:
            base_image.image.digest = normalize_artifact_sha256_digest(
                base_image.image.digest,
                label="artifact base image digest",
            )
            if not base_image.source_repository or not base_image.source_ref:
                raise ValueError(
                    "artifact manifest schema_version 2 requires base image source provenance"
                )
            base_image.source_repository = normalize_artifact_repository_identity(
                base_image.source_repository,
                label="artifact base image source_repository",
            )
            base_image.source_ref = normalize_artifact_git_commit(
                base_image.source_ref,
                label="artifact base image source_ref",
            )

        if not self.build_provenance.build_tools:
            raise ValueError("artifact manifest schema_version 2 requires build tool provenance")
        build_tool_names: set[str] = set()
        for build_tool in self.build_provenance.build_tools:
            normalized_name = build_tool.name.casefold()
            if normalized_name in build_tool_names:
                raise ValueError(
                    "artifact manifest schema_version 2 cannot contain duplicate build tools"
                )
            build_tool_names.add(normalized_name)
            if not build_tool.source_repository or not build_tool.source_ref:
                raise ValueError(
                    "artifact manifest schema_version 2 requires exact build tool source provenance"
                )
            build_tool.source_repository = normalize_artifact_repository_identity(
                build_tool.source_repository,
                label="artifact build tool source_repository",
            )
            build_tool.source_ref = normalize_artifact_git_commit(
                build_tool.source_ref,
                label="artifact build tool source_ref",
            )

        tenant_lock = next(
            lock for lock in dependency_provenance.uv_locks if lock.scope == "tenant"
        )
        if tenant_lock.source_ref != self.source_commit:
            raise ValueError("artifact manifest tenant uv lock source_ref must match source_commit")
        support_lock = next(
            lock for lock in dependency_provenance.uv_locks if lock.scope == "support_runtime"
        )
        build_tool_sources = {
            (build_tool.source_repository, build_tool.source_ref)
            for build_tool in self.build_provenance.build_tools
        }
        if (support_lock.source_repository, support_lock.source_ref) not in build_tool_sources:
            raise ValueError(
                "artifact manifest support_runtime uv lock must match build tool provenance"
            )

        for addon_source in self.addon_sources:
            addon_source.repository = normalize_artifact_repository_identity(
                addon_source.repository,
                label="artifact addon source repository",
            )
            addon_source.ref = normalize_artifact_git_commit(
                addon_source.ref,
                label="artifact addon source ref",
            )
        for addon_selector in self.addon_selectors:
            addon_selector.repository = normalize_artifact_repository_identity(
                addon_selector.repository,
                label="artifact addon selector repository",
            )
            addon_selector.resolved_ref = normalize_artifact_git_commit(
                addon_selector.resolved_ref,
                label="artifact addon selector resolved_ref",
            )

        if (
            self.openupgrade_inputs.addon_repository.strip()
            or self.openupgrade_inputs.install_spec.strip()
        ):
            raise ValueError(
                "artifact manifest schema_version 2 requires typed dependency provenance instead of openupgrade_inputs"
            )
        return self


def artifact_manifest_matches_image_repository(
    manifest: ArtifactIdentityManifest,
    *,
    expected_repository: str,
) -> bool:
    normalized_expected = expected_repository.strip().rstrip("/")
    normalized_observed = manifest.image.repository.strip().rstrip("/")
    return bool(normalized_expected and normalized_observed == normalized_expected)
