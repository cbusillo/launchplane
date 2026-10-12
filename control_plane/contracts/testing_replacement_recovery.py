"""Private, value-free dispatch references for Launchplane's testing replacements."""

from pydantic import BaseModel, ConfigDict, Field


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
