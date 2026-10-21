"""Reviewed inputs for a fresh application and its independently owned state."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from control_plane.contracts.application_state_path import require_application_state_path


class TestingApplicationCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    binding_key: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,127}$")
    encoding: Literal["urlsafe", "base64", "hex"]


class TestingApplicationState(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["none", "volume"]
    container_path: str = ""

    @model_validator(mode="after")
    def shape(self) -> Self:
        if self.kind == "none":
            if self.container_path:
                raise ValueError("Stateless activation cannot allocate or bind state.")
            return self
        require_application_state_path(self.container_path)
        return self


class TestingApplicationSpecification(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_port: int = Field(ge=1, le=65535)
    health_path: str = Field(pattern=r"^/[^\s?#]*$")
    network_name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    state: TestingApplicationState
    local_credentials: tuple[TestingApplicationCredential, ...] = ()

    @model_validator(mode="after")
    def distinct_bindings(self) -> Self:
        keys = [item.binding_key for item in self.local_credentials]
        if self.health_path.startswith("//"):
            raise ValueError("Health requires an application-relative path.")
        if len(keys) != len(set(keys)):
            raise ValueError("Local credential bindings must be distinct.")
        return self
