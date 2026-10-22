"""Value-free bridge capacity observations and clean pre-create refusals."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


HOST_BRIDGE_NETWORK_FAILURE_MESSAGES = {
    "host_bridge_network_capacity_short": "The selected host has insufficient free bridge networks for this create and its safety margin.",
    "host_bridge_network_capacity_unavailable": "The selected host's free bridge-network capacity could not be verified.",
}


PREVIEW_MEMORY_FAILURE_CODE = "preview_memory_admission_refused"
PREVIEW_MEMORY_FAILURE_MESSAGES = {
    PREVIEW_MEMORY_FAILURE_CODE: "New preview admission was refused because execution-host memory could not meet or verify its configured floor.",
}


class BridgeNetworkCapacity(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total_slots: int = Field(ge=0, strict=True)
    free_slots: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def consistent(self) -> "BridgeNetworkCapacity":
        if self.free_slots > self.total_slots:
            raise ValueError("Free bridge capacity exceeds the pool capacity.")
        return self


class HostNetworkCapacityObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server_id: str = ""
    observed_at: str
    status: Literal["available", "short", "unavailable", "denied"]
    capacity: BridgeNetworkCapacity | None = None
    required_networks: int = Field(ge=1)
    safety_margin: int = Field(ge=0)
    reason: str = ""
