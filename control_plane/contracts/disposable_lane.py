"""Recorded safety facts for ordinary disposable-lane work, never access grants."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DisposableLaneSafety(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str = Field(min_length=1)
    instance: str = Field(min_length=1)
    observed_by: str = Field(min_length=1)
    observer_kind: Literal["director_agent"]
    binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    usage: Literal["unknown", "unused", "in_use"] = "unknown"
    data: Literal["unknown", "reproducible", "only_copy"] = "unknown"
    production_resources: Literal["unknown", "isolated", "separation_ready", "shared"] = "unknown"
    usage_evidence: str = ""
    data_evidence: str = ""
    isolation_evidence: str = ""
    replaces_operation_id: str = ""
    replaced_target_id: str = ""
    observed_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def validate_evidence(self) -> "DisposableLaneSafety":
        if self.observed_at.utcoffset() is None or self.expires_at.utcoffset() is None:
            raise ValueError("Disposable safety observations require timezone-aware timestamps.")
        if not 0 < (self.expires_at - self.observed_at).total_seconds() <= 900:
            raise ValueError("Disposable safety evidence lasts at most fifteen minutes.")
        for fact, reference in (
            (self.usage, self.usage_evidence),
            (self.data, self.data_evidence),
            (self.production_resources, self.isolation_evidence),
        ):
            if fact != "unknown" and not reference.strip():
                raise ValueError("Known disposable safety facts require their source evidence.")
        if bool(self.replaces_operation_id) != bool(self.replaced_target_id):
            raise ValueError("Replacement evidence names both the operation and retained target.")
        return self


class DisposableLaneObservationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["inspect", "record", "prepare-rebuild"] = "inspect"
    product: str = Field(min_length=1)
    context: str = Field(min_length=1)
    instance: str = Field(min_length=1)
    preview_id: str = ""
    expected_binding_sha256: str = ""
    usage: Literal["unknown", "unused", "in_use"] = "unknown"
    data: Literal["unknown", "reproducible", "only_copy"] = "unknown"
    production_resources: Literal["unknown", "isolated", "separation_ready", "shared"] = "unknown"
    usage_evidence: str = ""
    data_evidence: str = ""
    isolation_evidence: str = ""
    replaces_operation_id: str = ""
    lifetime_seconds: int = Field(default=300, gt=0, le=900)


class DisposableTestingReplacementPreparation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retained_target_id: str = Field(min_length=1)
    prepared_by: str = Field(min_length=1)
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_preparation(self) -> "DisposableTestingReplacementPreparation":
        if self.recorded_at.utcoffset() is None:
            raise ValueError("Replacement preparation requires a timezone-aware timestamp.")
        return self


class DisposableTestingSupersession(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    retained_target_id: str = Field(min_length=1)
    replacement_target_id: str = Field(min_length=1)
    evidence_reference: str = Field(min_length=1)
    recorded_at: datetime

    @model_validator(mode="after")
    def validate_replacement(self) -> "DisposableTestingSupersession":
        if self.recorded_at.utcoffset() is None:
            raise ValueError("Supersession requires a timezone-aware timestamp.")
        if self.retained_target_id == self.replacement_target_id:
            raise ValueError("Disposable rebuilding replaces the retained target.")
        return self
