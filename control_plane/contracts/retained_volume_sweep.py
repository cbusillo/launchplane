from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256


RETAINED_VOLUME_SWEEP_ROUTE = "/v1/retained-volume-sweep"


class RetainedVolumeSweepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    retirement_record_id: str = Field(min_length=1, max_length=200)
    minimum_age_seconds: int = Field(default=86400, ge=1, le=31536000)
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def validate_review(self) -> "RetainedVolumeSweepRequest":
        self.retirement_record_id = self.retirement_record_id.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        if self.mode in {"apply", "readback"} and len(self.reviewed_plan_sha256) != 64:
            raise ValueError(
                "Apply and readback require the original reviewed retained-volume plan digest."
            )
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Dry-run rejects a reviewed plan digest.")
        return self


class RetainedVolumeSweepItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volume_identity_sha256: str
    status: Literal["eligible", "held", "removed", "absent", "unknown"]
    reason: str


class RetainedVolumeSweepPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    retirement_record_id: str
    minimum_age_seconds: int
    items: tuple[RetainedVolumeSweepItem, ...]

    def digest(self) -> str:
        return nonvalidating_compact_sorted_json_sha256(self.model_dump(mode="json"))


class RetainedVolumeSweepResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: dict[str, str] = Field(default_factory=dict)
    plan_sha256: str
    items: tuple[RetainedVolumeSweepItem, ...]
    replayed: bool | None = Field(
        default=None, json_schema_extra={"x-launchplane-optional-response": True}
    )
