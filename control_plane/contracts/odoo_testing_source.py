"""Private production-read and captured-artifact identities for isolated testing."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256

NATIVE_TESTING_COPY_CHUNK_BYTES = 64 * 1024
NATIVE_TESTING_COPY_CACHE_DIRECTORY = "private-testing-source"


class NativeOdooSourceAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product: str
    context: str
    instance: Literal["prod"] = "prod"
    target_id: str = Field(min_length=1)
    database_name: str = Field(pattern=r"^[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,62}$")
    filestore_path: str = Field(pattern=r"^/[a-zA-Z0-9_./-]+$")
    settings_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")

    @property
    def binding_sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))


class NativeOdooTestingSource(NativeOdooSourceAuthority):
    server_id: str = Field(min_length=1)
    compose_project: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")
    database_container: str = Field(pattern=r"^[a-f0-9]{64}$")
    web_container: str = Field(pattern=r"^[a-f0-9]{64}$")
    database_image: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    web_image: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")

    @property
    def binding_sha256(self) -> str:
        # Restarted containers are observations of the same registered source.
        # Its reviewed authority changes only when its service records change.
        return NativeOdooSourceAuthority.model_validate(
            self.model_dump(mode="json", include=set(NativeOdooSourceAuthority.model_fields))
        ).binding_sha256


class NativeTestingCopyArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size: int = Field(gt=0)


class NativeTestingCopyCapture(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    generation_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    capture_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    database: NativeTestingCopyArtifact
    filestore: NativeTestingCopyArtifact
    captured_at: str
    source_observation: NativeOdooTestingSource

    @model_validator(mode="after")
    def _validate_source(self) -> "NativeTestingCopyCapture":
        if (
            self.source_observation.product != self.product
            or self.source_observation.binding_sha256 != self.source_binding_sha256
        ):
            raise ValueError(
                "The captured source observation differs from its registered authority."
            )
        return self
