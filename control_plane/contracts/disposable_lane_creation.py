from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class DisposableLaneCreation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created_by: Literal[
        "service:fresh-testing-bootstrap",
        "service:testing-replacement-resources",
        "service:odoo-preview",
    ]
    lane_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    replaces_operation_id: str = ""
    replaced_target_id: str = ""
    isolation_evidence: str = ""
    binding_sha256: str = ""
    qualified_at: str = ""
    recipient_state: str = ""
    data: Literal["reproducible", "only_copy", "unknown"]
    production_resources: Literal["isolated", "separation_ready", "shared", "unknown"]
