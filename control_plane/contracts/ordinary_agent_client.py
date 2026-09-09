"""Compiled, bounded client requests; approval provenance is service-owned."""

from typing import Literal

from pydantic import Field, model_validator

from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget, StrictFrozenModel
from control_plane.contracts.ordinary_agent_lifecycle import OrdinaryAgentDeliveryBinding
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentSessionAttenuation,
    OrdinaryAgentSessionOperationView,
)

ORDINARY_AGENT_ENROLLMENT_PROPOSE_ACTION = "ordinary_agent_enrollment.propose"


class OrdinaryAgentEnrollmentClientRequest(StrictFrozenModel):
    descriptor_id: Literal["ordinary-agent-enrollment"] = "ordinary-agent-enrollment"
    operation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    action: Literal["enroll", "rotate_credential"]
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    target: OrdinaryAgentTarget
    github_app_id: int = Field(gt=0, le=2**63 - 1)
    secret_binding_id: str = Field(min_length=1, max_length=256)
    credential_valid_from: int = Field(ge=0, le=2**63 - 1)
    credential_expires_at: int = Field(ge=1, le=2**63 - 1)
    delivery: OrdinaryAgentDeliveryBinding
    session_attenuation: OrdinaryAgentSessionAttenuation | None = None

    @model_validator(mode="after")
    def validate_lifetimes(self) -> "OrdinaryAgentEnrollmentClientRequest":
        if not (
            self.credential_valid_from < self.delivery.expires_at <= self.credential_expires_at
        ):
            raise ValueError("delivery must fit inside the requested credential lifetime")
        if (
            self.session_attenuation is not None
            and self.session_attenuation.session_expires_at > self.credential_expires_at
        ):
            raise ValueError("requested session cannot outlive the credential")
        return self


class OrdinaryAgentSessionClientRequest(StrictFrozenModel):
    descriptor_id: Literal["ordinary-agent-session"] = "ordinary-agent-session"
    operation_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
    attenuation: OrdinaryAgentSessionAttenuation


class OrdinaryAgentOperationClientResponse(StrictFrozenModel):
    schema_version: Literal[1] = 1
    operation: OrdinaryAgentSessionOperationView
    review_url: str


class OrdinaryAgentDisconnectRequest(StrictFrozenModel):
    source_event_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{2,127}$")
