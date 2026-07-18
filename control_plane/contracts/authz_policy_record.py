from __future__ import annotations

import hashlib
import json
from typing import Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.service_auth import LaunchplaneAuthzPolicy


AuthzPolicyStatus = Literal["active", "superseded"]


def authz_policy_sha256(policy: LaunchplaneAuthzPolicy) -> str:
    payload = policy.model_dump(mode="json", exclude_none=True)
    if not policy.terminal_agents:
        payload.pop("terminal_agents", None)
    if not policy.local_operators:
        payload.pop("local_operators", None)
    if not policy.local_admins:
        payload.pop("local_admins", None)
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build_authz_policy_record_id(*, revision: int, policy_sha256: str) -> str:
    if revision < 1:
        raise ValueError("authz policy record revision must be positive")
    return f"launchplane-authz-policy-r{revision:020d}-{policy_sha256[:12]}"


class LaunchplaneAuthzPolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    revision: int = Field(default=1, ge=1)
    status: AuthzPolicyStatus = "active"
    source: str
    updated_at: str
    policy_sha256: str = Field(default="")
    policy: LaunchplaneAuthzPolicy
    audit: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_record(self) -> "LaunchplaneAuthzPolicyRecord":
        if not self.record_id.strip():
            raise ValueError("authz policy record requires record_id")
        if not self.source.strip():
            raise ValueError("authz policy record requires source")
        if not self.updated_at.strip():
            raise ValueError("authz policy record requires updated_at")
        computed_sha256 = authz_policy_sha256(self.policy)
        if not self.policy_sha256:
            self.policy_sha256 = computed_sha256
        if self.policy_sha256 != computed_sha256:
            raise ValueError("authz policy record policy_sha256 does not match policy payload")
        return self


AuthzPolicyCompareWriteStatus = Literal[
    "written",
    "unchanged",
    "stale",
    "missing",
    "ambiguous_active",
    "replayed",
    "idempotency_conflict",
    "reservation_in_progress",
    "reconciliation_required",
]


class AuthzPolicyCompareWriteResult(NamedTuple):
    status: AuthzPolicyCompareWriteStatus
    current_record: LaunchplaneAuthzPolicyRecord | None = None
    idempotency_record: LaunchplaneIdempotencyRecord | None = None
