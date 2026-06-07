from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

EdgeEndpointProvider = Literal["dokploy"]
EdgeEndpointStatus = Literal["active", "disabled"]
EdgeEndpointUpstreamHostKind = Literal["ip"]
EdgeEndpointUpstreamScheme = Literal["http", "https"]


class EdgeEndpointRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    endpoint_key: str
    provider: EdgeEndpointProvider = "dokploy"
    server_name: str
    upstream_host: str
    upstream_host_kind: EdgeEndpointUpstreamHostKind = "ip"
    upstream_scheme: EdgeEndpointUpstreamScheme = "https"
    upstream_port: int = Field(ge=1, le=65535)
    status: EdgeEndpointStatus = "active"
    source_label: str = ""
    updated_at: str

    @field_validator(
        "endpoint_key",
        "server_name",
        "upstream_host",
        "updated_at",
        mode="after",
    )
    @classmethod
    def _validate_required_text(cls, value: str) -> str:
        normalized_value = value.strip()
        if not normalized_value:
            raise ValueError("edge endpoint record requires non-empty text fields")
        return normalized_value

    @field_validator("source_label", mode="after")
    @classmethod
    def _normalize_optional_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _validate_record(self) -> "EdgeEndpointRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported edge endpoint record schema version")
        if self.upstream_host_kind != "ip":
            raise ValueError("edge endpoint upstream_host_kind must be ip")
        if not _is_ipv4_address(self.upstream_host):
            raise ValueError("edge endpoint upstream_host must be an IPv4 address")
        return self


def _is_ipv4_address(value: str) -> bool:
    parts = value.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdecimal():
            return False
        if len(part) > 1 and part.startswith("0"):
            return False
        octet = int(part)
        if octet < 0 or octet > 255:
            return False
    return True
