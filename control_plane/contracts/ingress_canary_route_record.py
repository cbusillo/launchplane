from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


IngressCanaryRouteStatus = Literal["active", "disabled"]


class IngressCanaryRouteRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    canary_key: str
    product: str
    context: str
    domain_name: str
    expected_host_id: int = Field(ge=1)
    edge_endpoint_key: str
    certificate_id: int | Literal["new"]
    status: IngressCanaryRouteStatus = "active"
    source_label: str = ""
    updated_at: str

    @field_validator(
        "canary_key",
        "product",
        "context",
        "domain_name",
        "edge_endpoint_key",
        "updated_at",
        mode="after",
    )
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("ingress canary route record requires non-empty text fields")
        return normalized_value

    @field_validator("source_label", mode="after")
    @classmethod
    def _normalize_optional_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("canary_key", mode="after")
    @classmethod
    def _validate_canary_key(cls, value: str) -> str:
        if any(character.isspace() for character in value) or any(
            separator in value for separator in ("/", "\\")
        ):
            raise ValueError("ingress canary route key must be a single path-safe segment")
        return value

    @field_validator("domain_name", mode="after")
    @classmethod
    def _validate_domain_name(cls, value: str) -> str:
        domain_name = value.rstrip(".").lower()
        if (
            not domain_name
            or any(character.isspace() for character in domain_name)
            or any(separator in domain_name for separator in (":", "/", "\\", ","))
        ):
            raise ValueError("ingress canary route domain_name must be one domain name")
        return domain_name

    @model_validator(mode="after")
    def _validate_record(self) -> "IngressCanaryRouteRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported ingress canary route record schema version")
        if self.certificate_id != "new" and self.certificate_id < 0:
            raise ValueError("ingress canary route certificate_id must be non-negative")
        return self
