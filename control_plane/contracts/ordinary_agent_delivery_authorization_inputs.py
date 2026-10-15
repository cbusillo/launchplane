from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.repository_inventory import (
    required_token,
)


RepositoryInventoryProjectionState = Literal["complete", "unavailable", "truncated", "ambiguous"]


class AuthorizationCandidateInputDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str

    @model_validator(mode="after")
    def _validate(self) -> AuthorizationCandidateInputDiagnostic:
        self.code = required_token(self.code, "diagnostic code")
        self.message = required_token(self.message, "diagnostic message")
        return self
