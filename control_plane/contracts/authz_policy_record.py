from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.service_auth import LaunchplaneAuthzPolicy


AuthzPolicyStatus = Literal["active", "superseded"]


def authz_policy_sha256(policy: LaunchplaneAuthzPolicy) -> str:
    payload = policy.model_dump(mode="json", exclude_none=True)
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def build_authz_policy_record_id(*, updated_at: str, policy_sha256: str) -> str:
    timestamp = updated_at.replace("-", "").replace(":", "").replace("+00:00", "Z")
    return f"launchplane-authz-policy-{timestamp}-{policy_sha256[:12]}"


class LaunchplaneAuthzPolicyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    status: AuthzPolicyStatus = "active"
    source: str
    updated_at: str
    policy_sha256: str = Field(default="")
    policy: LaunchplaneAuthzPolicy

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

