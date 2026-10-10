"""Producer declarations, carried by the verified immutable build manifest."""

from pathlib import PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.artifact_dependency_provenance import normalize_artifact_relative_path


ReleaseInputKind = Literal[
    "static",
    "code",
    "manifest_assets",
    "database_data",
    "model",
    "migration",
    "dependency",
    "docs_ci",
    "unknown",
]


class ReleaseInputFile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: ReleaseInputKind
    module: str = Field(default="", pattern=r"^[a-zA-Z0-9_]*$")
    # For __manifest__.py, hash every parsed field except assets. An assets-only
    # change is safe only when this hash agrees across the two artifacts.
    manifest_database_sha256: str = Field(default="", pattern=r"^(|[0-9a-f]{64})$")

    @field_validator("path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        normalized = normalize_artifact_relative_path(value, label="release input file")
        if not PurePosixPath(normalized).parts:
            raise ValueError("release input must name a file")
        return normalized

    @model_validator(mode="after")
    def validate_path(self) -> "ReleaseInputFile":
        path = PurePosixPath(self.path)
        if self.kind == "manifest_assets" and (
            path.name != "__manifest__.py" or not self.manifest_database_sha256
        ):
            raise ValueError("assets manifest requires its non-assets semantic hash")
        return self


class ReleaseSourceInventory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_name: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: tuple[ReleaseInputFile, ...]

    @model_validator(mode="after")
    def validate_files(self) -> "ReleaseSourceInventory":
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("source inventory contains duplicate paths")
        return self


class ReleaseModuleDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-zA-Z0-9_]+$")
    depends: tuple[str, ...] = ()


class ArtifactReleaseCompatibility(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    complete: bool
    # The producer asserts that code/static-only deltas need no DB work and
    # that both versions can read AND write the unchanged database.
    read_write_compatible: bool
    sources: tuple[ReleaseSourceInventory, ...]
    modules: tuple[ReleaseModuleDeclaration, ...]
    # None is unexamined; an explicit empty tuple asserts no module DB work.
    examined_inputs_sha256: str = Field(default="", pattern=r"^(|[0-9a-f]{64})$")
    database_update_modules: tuple[str, ...] | None = None

    @model_validator(mode="after")
    def validate_inventory(self) -> "ArtifactReleaseCompatibility":
        names = [source.input_name for source in self.sources]
        modules = [module.name for module in self.modules]
        if len(names) != len(set(names)) or len(modules) != len(set(modules)):
            raise ValueError("release inventory input and module names must be unique")
        if any(
            file.module and file.module not in modules
            for source in self.sources
            for file in source.files
        ):
            raise ValueError("release input names an undeclared module")
        if any(
            dependency not in modules for module in self.modules for dependency in module.depends
        ):
            raise ValueError("release inventory must include the full module dependency graph")
        if self.database_update_modules is not None and (
            not self.examined_inputs_sha256 or not set(self.database_update_modules) <= set(modules)
        ):
            raise ValueError(
                "examined database plan requires the complete input hash and known modules"
            )
        return self


class ReleaseInputChange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_name: str
    path: str
    before_sha256: str = ""
    after_sha256: str = ""
    kind: ReleaseInputKind
    module: str = ""


class ReleaseDatabaseCompatibility(BaseModel):
    """A conservative result and module plan for exactly one release tuple."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    production_artifact_id: str
    candidate_artifact_id: str
    production_image: str
    candidate_image: str
    production_manifest_sha256: str
    candidate_manifest_sha256: str
    classification: Literal["compatible", "database_changing"]
    reasons: tuple[str, ...]
    changes: tuple[ReleaseInputChange, ...] = ()
    changed_modules: tuple[str, ...] = ()
    install_modules: tuple[str, ...] = ()
    update_modules: tuple[str, ...] = ()
    module_plan_complete: bool = False
    warm_assets: bool = False
    retain_previous_assets: bool = False
    preserve_noupdate: Literal[True] = True
