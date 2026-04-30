from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProductImageProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str

    @model_validator(mode="after")
    def _validate_image(self) -> "ProductImageProfile":
        if not self.repository.strip():
            raise ValueError("product image profile requires repository")
        return self


class ProductLaneProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instance: str
    context: str
    base_url: str = ""
    health_url: str = ""

    @model_validator(mode="after")
    def _validate_lane(self) -> "ProductLaneProfile":
        if not self.instance.strip():
            raise ValueError("product lane profile requires instance")
        if not self.context.strip():
            raise ValueError("product lane profile requires context")
        return self


class ProductPreviewProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    context: str = ""
    slug_template: str = "pr-{number}"

    @model_validator(mode="after")
    def _validate_preview(self) -> "ProductPreviewProfile":
        if self.enabled and not self.context.strip():
            raise ValueError("enabled product preview profile requires context")
        if self.enabled and "{number}" not in self.slug_template:
            raise ValueError("enabled product preview profile slug_template requires {number}")
        return self


class LaunchplaneProductProfileRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    display_name: str
    repository: str
    driver_id: str
    image: ProductImageProfile
    runtime_port: int = Field(ge=1, le=65535)
    health_path: str
    lanes: tuple[ProductLaneProfile, ...] = ()
    preview: ProductPreviewProfile = Field(default_factory=ProductPreviewProfile)
    updated_at: str
    source: str

    @model_validator(mode="after")
    def _validate_record(self) -> "LaunchplaneProductProfileRecord":
        if not self.product.strip():
            raise ValueError("product profile requires product")
        if not self.display_name.strip():
            raise ValueError("product profile requires display_name")
        if not self.repository.strip():
            raise ValueError("product profile requires repository")
        if not self.driver_id.strip():
            raise ValueError("product profile requires driver_id")
        if not self.health_path.startswith("/"):
            raise ValueError("product profile health_path must start with /")
        if not self.updated_at.strip():
            raise ValueError("product profile requires updated_at")
        if not self.source.strip():
            raise ValueError("product profile requires source")
        return self
