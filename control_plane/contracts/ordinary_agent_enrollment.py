from __future__ import annotations


from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget


class OrdinaryAgentPolicyBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    record_id: str = Field(min_length=1, max_length=256)
    revision: int = Field(ge=1, le=2**63 - 1)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    managed_set_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    managed_rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    target: OrdinaryAgentTarget
