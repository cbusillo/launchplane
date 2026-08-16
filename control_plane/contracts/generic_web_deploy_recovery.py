from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.drivers.generic_web_dispatch import GenericWebDeployEnvelope


GenericWebDeployRecoveryAction = Literal[
    "replay_completed",
    "wait_for_active_lease",
    "adopt_observed",
    "retry_original_operation",
    "hold_unknown",
]
GenericWebDeployRecoveryProviderOutcome = Literal[
    "present",
    "absent",
    "unknown",
    "not_inspected",
]


class GenericWebDeployRecoveryDryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: str
    instance: str
    original_deploy: GenericWebDeployEnvelope
    reason: str = Field(max_length=1000)

    @model_validator(mode="after")
    def _validate_request(self) -> GenericWebDeployRecoveryDryRunRequest:
        self.product = self.product.strip()
        self.instance = self.instance.strip()
        self.reason = self.reason.strip()
        if not self.product:
            raise ValueError("Generic web deploy recovery requires product.")
        if not self.instance:
            raise ValueError("Generic web deploy recovery requires instance.")
        if not self.reason:
            raise ValueError("Generic web deploy recovery requires reason.")
        if self.original_deploy.product.strip() != self.product:
            raise ValueError("Generic web deploy recovery requires matching product values.")
        if self.original_deploy.deploy.instance.strip() != self.instance:
            raise ValueError("Generic web deploy recovery requires matching instance values.")
        return self


class GenericWebDeployRecoveryDryRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    status: Literal["ok"] = "ok"
    mode: Literal["dry-run"] = "dry-run"
    product: str
    context: str
    instance: str
    reservation_state: Literal["running", "completed", "reconcile_required"]
    reservation_attempt: int = Field(ge=1)
    reservation_created_at: str
    reservation_updated_at: str
    reservation_lease_expires_at: str = ""
    observed_at: str
    reconciliation_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_target_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_effect_phase: str = Field(max_length=128)
    provider_outcome: GenericWebDeployRecoveryProviderOutcome
    provider_status: str = Field(default="", max_length=128)
    retry_safe: bool
    proposed_action: GenericWebDeployRecoveryAction
    recovery_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


def generic_web_deploy_recovery_identifier_sha256(value: str) -> str:
    return hashlib.sha256(value.strip().encode("utf-8")).hexdigest()


def build_generic_web_deploy_recovery_digest(payload: dict[str, object]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
