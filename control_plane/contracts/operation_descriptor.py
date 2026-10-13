from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


OperationEffect = Literal[
    "observation", "inert_evidence", "operation", "access", "credential", "destructive", "live_site"
]
OperationScope = Literal["global", "context", "instance", "preview"]


class OperationDescriptor(BaseModel):
    """Source-owned route metadata; absent mode effects confer no observation access."""

    model_config = ConfigDict(extra="forbid")

    method: Literal["GET", "POST"]
    route_path: str
    authz_action: str = ""
    scope: OperationScope
    mode_effects: dict[str, OperationEffect] = Field(default_factory=dict)
