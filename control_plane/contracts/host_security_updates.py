from typing import Literal
import time
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.public_ingress_monitoring import health_monitor_freshness_seconds
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256

from pydantic import BaseModel, ConfigDict, Field, model_validator

SECURITY_UPDATE_MAX_AGE_SECONDS = 2 * 24 * 60 * 60
SECURITY_UPDATE_COMPLETION_SECONDS = 30
HOST_SECURITY_UPDATE_CHECK = "host-security-updates"


class HostSecurityUpdateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    status: Literal["observed", "missing", "unknown", "denied"] = "unknown"
    observed_at: float = Field(ge=0)
    completed_at: float | None = Field(default=None, ge=0)
    server_id: str = ""
    provider_target_sha256: str = ""

    @model_validator(mode="after")
    def validate_completion(self) -> "HostSecurityUpdateEvidence":
        if self.status == "observed" and (
            self.completed_at is None or self.completed_at > self.observed_at
        ):
            raise ValueError("Verified update evidence requires a past completion.")
        if self.status in {"denied", "missing"} and self.completed_at is not None:
            raise ValueError("Unverified update evidence cannot include a completion.")
        return self


class HostSecurityUpdateObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    evidence: HostSecurityUpdateEvidence


def host_security_update_warnings(
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    *,
    now: float | None = None,
) -> tuple[str, ...]:
    if lane.instance != "prod" or profile.production_use != "live":
        return ()
    read = getattr(record_store, "read_host_security_update_observation", None)
    current_time = time.time() if now is None else now
    evidence = None
    try:
        if callable(read):
            evidence = read(context_name=lane.context, instance_name=lane.instance).evidence
        target_reader = getattr(record_store, "read_provider_target_record", None)
        target = (
            target_reader(context_name=lane.context, instance_name=lane.instance)
            if callable(target_reader)
            else None
        )
        if (
            evidence is not None
            and evidence.status == "observed"
            and evidence.completed_at is not None
            and 0
            <= current_time - evidence.observed_at
            <= health_monitor_freshness_seconds(lane.instance)
            and 0 <= current_time - evidence.completed_at <= SECURITY_UPDATE_MAX_AGE_SECONDS
            and target is not None
            and evidence.provider_target_sha256
            == nonvalidating_compact_sorted_json_sha256(target.model_dump(mode="json"))
        ):
            return ()
    except (ValueError, OSError):
        evidence = None
    if evidence is not None and evidence.status == "denied":
        return ("The provider denied the production host security-update read.",)
    if evidence is not None and evidence.status == "missing":
        return ("A production host has no recorded successful security-update run.",)
    if (
        evidence is not None
        and evidence.completed_at is not None
        and current_time - evidence.completed_at > SECURITY_UPDATE_MAX_AGE_SECONDS
    ):
        return ("A production host has not completed security updates in more than two days.",)
    return ("Production host security-update completion could not be verified.",)
