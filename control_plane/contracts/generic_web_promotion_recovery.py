"""Redacted, existing-only recovery of a Client's generic-web promotion."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


PromotionRecoveryAction = Literal[
    "replay_completed", "wait_for_active_lease", "adopt_promotion", "adopt_rollback", "hold_unknown"
]


class PromotionRecoveryReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000, pattern=r"\S")


class PromotionRecoveryApply(PromotionRecoveryReview):
    recovery_reference: str = Field(min_length=1)
    expected_recovery_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PromotionRecoveryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    instance: Literal["prod"] = "prod"
    recovery_reference: str
    reservation_state: Literal["running", "reconcile_required", "completed"]
    reservation_attempt: int
    checkpoint: str
    proposed_action: PromotionRecoveryAction
    recovery_digest: str


class PromotionRecoveryApplied(PromotionRecoveryPlan):
    status: Literal["accepted"] = "accepted"
