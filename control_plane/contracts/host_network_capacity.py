"""Value-free bridge capacity observations and clean pre-create refusals."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


HOST_BRIDGE_NETWORK_FAILURE_MESSAGES = {
    "host_bridge_network_capacity_short": "The selected host has insufficient free bridge networks for this create and its safety margin.",
    "host_bridge_network_capacity_unavailable": "The selected host's free bridge-network capacity could not be verified.",
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
