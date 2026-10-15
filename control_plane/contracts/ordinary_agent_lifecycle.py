from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent_enrollment import (
    OrdinaryAgentPolicyBinding,
)


LifecycleRecordStatus = Literal["active", "superseded", "revoked"]


class StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OrdinaryAgentAuthenticationCredentialRecord(StrictFrozenModel):
    schema_version: Literal[1] = 1
    record_id: str = Field(min_length=1, max_length=256)
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_version: int = Field(ge=1, le=2**63 - 1)
    status: LifecycleRecordStatus
    credential_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid_from: int = Field(ge=0, le=2**63 - 1)
    expires_at: int = Field(ge=0, le=2**63 - 1)
    revoked_at: int | None = Field(default=None, ge=0, le=2**63 - 1)
    issuance_evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy: OrdinaryAgentPolicyBinding
    recorded_at: str = Field(min_length=1, max_length=64)
    record_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record(self) -> OrdinaryAgentAuthenticationCredentialRecord:
        if self.expires_at <= self.valid_from:
            raise ValueError("agent authentication credential expiry must follow valid_from")
        if self.status == "revoked" and self.revoked_at is None:
            raise ValueError("revoked authentication credentials require revoked_at")
        if self.status != "revoked" and self.revoked_at is not None:
            raise ValueError("only revoked authentication credentials may set revoked_at")
        if self.record_sha256 != lifecycle_record_sha256(self, digest_field="record_sha256"):
            raise ValueError(
                "ordinary-agent authentication credential digest does not match payload"
            )
        return self


def lifecycle_record_sha256(record: BaseModel, *, digest_field: str) -> str:
    return canonical_json_sha256(record.model_dump(mode="json", exclude={digest_field}))
