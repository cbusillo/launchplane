"""Value-free cycle facts; recording these facts does not qualify a retirement."""

from collections.abc import Mapping
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


class SyncCycleHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["idle", "running", "success", "failed", "unknown", "unavailable"]
    mode: Literal["full", "incremental"] | None = None
    cycle_id: str | None = Field(default=None, max_length=36)
    cycle_started_at: str | None = Field(default=None, max_length=64)
    cycle_finished_at: str | None = Field(default=None, max_length=64)
    last_heartbeat: str | None = Field(default=None, max_length=64)
    is_stale: bool
    next_cycle_overdue: bool
    freshness_status: Literal["unknown", "unavailable", "fresh", "stale", "overdue", "failed"]
    stale_threshold_seconds: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_cycle(self) -> "SyncCycleHealth":
        if self.cycle_id is not None:
            identity = UUID(self.cycle_id)
            if self.cycle_id not in {identity.hex, str(identity)}:
                raise ValueError("Cycle identity must be a canonical UUID.")
        started = _timestamp(self.cycle_started_at)
        finished = _timestamp(self.cycle_finished_at)
        heartbeat = _timestamp(self.last_heartbeat)
        if self.status in {"running", "success", "failed"}:
            if not (self.cycle_id and self.mode and started):
                raise ValueError("An active or completed cycle requires identity, mode and start.")
        if self.status in {"success", "failed"} and finished is None:
            raise ValueError("A completed cycle requires its finish time.")
        if self.status == "running" and finished is not None:
            raise ValueError("A running cycle cannot have a finish time.")
        if (finished is not None or heartbeat is not None) and started is None:
            raise ValueError("Cycle timestamps require a start time.")
        if started is not None:
            if finished is not None and finished < started:
                raise ValueError("Cycle finish precedes its start.")
            if heartbeat is not None and heartbeat < started:
                raise ValueError("Cycle heartbeat precedes its start.")
        if self.is_stale and self.status not in {"running", "unavailable"}:
            raise ValueError("Stale state does not match the cycle state.")
        if self.next_cycle_overdue and self.status != "success":
            raise ValueError("Only a successful cycle can await an overdue successor.")
        expected = "fresh"
        if self.is_stale:
            expected = "stale"
        if self.next_cycle_overdue:
            expected = "overdue"
        if self.status in {"unknown", "unavailable", "failed"}:
            expected = self.status
        if self.freshness_status != expected:
            raise ValueError("Freshness does not match the reported cycle facts.")
        return self


class SyncCycleHealthEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["recorded", "malformed"]
    cycle: SyncCycleHealth | None = None

    @model_validator(mode="after")
    def validate_evidence(self) -> "SyncCycleHealthEvidence":
        if (self.status == "recorded") != (self.cycle is not None):
            raise ValueError("Only recorded evidence includes cycle facts.")
        return self


def _timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.utcoffset() is None:
        raise ValueError("Cycle timestamps must include a timezone.")
    return parsed


def sync_cycle_health_evidence_from_payload(payload: object) -> SyncCycleHealthEvidence | None:
    if not isinstance(payload, Mapping) or "sync" not in payload:
        return None
    reported = payload["sync"]
    if isinstance(reported, Mapping):
        # Never retain errors, business rows, model names or unrecognized endpoint fields.
        selected = {
            name: reported[name] for name in SyncCycleHealth.model_fields if name in reported
        }
        try:
            return SyncCycleHealthEvidence(
                status="recorded", cycle=SyncCycleHealth.model_validate(selected)
            )
        except (ValidationError, ValueError):
            pass
    return SyncCycleHealthEvidence(status="malformed")
