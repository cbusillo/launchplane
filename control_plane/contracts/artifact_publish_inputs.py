from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _looks_like_owner_repo(value: str) -> bool:
    owner, separator, name = value.partition("/")
    return bool(owner and separator and name and "/" not in name and len(value.split()) == 1)


class GenericArtifactPublishInputsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str = "testing"
    pr_number: int | None = Field(default=None, ge=1)
    preview_slug: str = ""
    source_git_ref: str = ""
    isolated: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericArtifactPublishInputsRequest":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.preview_slug = self.preview_slug.strip()
        self.source_git_ref = self.source_git_ref.strip()
        if not self.context:
            raise ValueError("Artifact publish inputs request requires context.")
        if not self.instance:
            raise ValueError("Artifact publish inputs request requires instance.")
        return self


class GenericArtifactPublishInputsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    environment: dict[str, str] = Field(default_factory=dict)
    product: str = ""
    repository: str = ""
    image_repository: str = ""
    devkit_repository: str = ""
    shared_addons_repository: str = ""
    preview_slug: str = ""
    source_git_ref: str = ""
    image_tag: str = ""

    @model_validator(mode="after")
    def _validate_result(self) -> "GenericArtifactPublishInputsResult":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.environment = {
            key.strip(): value.strip()
            for key, value in self.environment.items()
            if key.strip() and value.strip()
        }
        self.product = self.product.strip()
        self.repository = self.repository.strip()
        self.image_repository = self.image_repository.strip().rstrip("/")
        self.devkit_repository = self.devkit_repository.strip()
        self.shared_addons_repository = self.shared_addons_repository.strip()
        self.preview_slug = self.preview_slug.strip()
        self.source_git_ref = self.source_git_ref.strip()
        self.image_tag = self.image_tag.strip()
        if not self.context:
            raise ValueError("Artifact publish inputs result requires context.")
        if not self.instance:
            raise ValueError("Artifact publish inputs result requires instance.")
        if self.image_tag and not self.image_repository:
            raise ValueError(
                "Artifact publish inputs result requires image_repository when image_tag is set."
            )
        if bool(self.devkit_repository) != bool(self.shared_addons_repository):
            raise ValueError(
                "Artifact publish inputs result requires devkit_repository and "
                "shared_addons_repository together."
            )
        for field_name, repository in (
            ("repository", self.repository),
            ("devkit_repository", self.devkit_repository),
            ("shared_addons_repository", self.shared_addons_repository),
        ):
            if repository and not _looks_like_owner_repo(repository):
                raise ValueError(
                    f"Artifact publish inputs result {field_name} must be formatted as owner/name."
                )
        return self
