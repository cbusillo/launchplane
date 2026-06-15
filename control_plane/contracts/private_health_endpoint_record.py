from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.notifications import public_url_error


PrivateHealthEndpointStatus = Literal["active", "disabled"]


class PrivateHealthEndpointRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    endpoint_key: str
    product: str
    context: str
    instance: str
    url: str
    status: PrivateHealthEndpointStatus = "active"
    source_label: str = ""
    updated_at: str

    @field_validator(
        "endpoint_key",
        "product",
        "context",
        "instance",
        "url",
        "updated_at",
        mode="after",
    )
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("private health endpoint record requires non-empty text fields")
        return normalized_value

    @field_validator("source_label", mode="after")
    @classmethod
    def _normalize_optional_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate_record(self) -> "PrivateHealthEndpointRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported private health endpoint record schema version")
        public_error = public_url_error(self.url)
        if public_error == "invalid_url":
            raise ValueError("private health endpoint url must be a valid HTTP URL")
        if not public_error:
            raise ValueError("private health endpoint url must not be public")
        return self
