"""Private, value-free dispatch references for Launchplane's testing replacements."""

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class TestingReplacementRecoveryRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["refused", "eligible"]
    reason: Literal[
        "",
        "recovery_scope_unsupported",
        "recovery_operation_not_held",
        "recovery_execution_exclusion_unavailable",
        "recovery_operation_changed",
        "recovery_authorization_changed",
        "recovery_journal_absent",
        "recovery_attempt_changed",
        "recovery_binding_changed",
        "recovery_dispatch_unacknowledged",
        "recovery_effect_unsupported",
        "recovery_inspection_before_fence",
        "recovery_provider_evidence_incomplete",
        "recovery_operation_or_binding_changed",
        "recovery_evidence_unavailable",
    ]
    journal: Literal["present", "absent", "unknown"]
    attempt_matches: bool | None
    binding_matches: bool | None
    effect_count: int = Field(ge=0)
    effects_acknowledged: bool | None
    provider_effects: Literal["unknown", "quiescent_observed"]
    provider_inspected: bool
    safe_to_release: Literal[False] = False


class TestingReplacementEffect(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: str
    path: str
    dispatched_at: str
    acknowledged: bool = False
    target_id: str = ""
    schedule_id: str = ""
    deployment_title: str = ""


class TestingReplacementRecoveryJournal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attempt: int = Field(ge=1)
    target_fingerprint: str
    target_id: str
    provider_fingerprint: str = ""
    effects: tuple[TestingReplacementEffect, ...] = ()
    inspection: dict[str, str] = Field(default_factory=dict)
