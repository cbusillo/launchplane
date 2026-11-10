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


class NativeTestingSweepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["native-testing"]
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    product: str = Field(min_length=1, max_length=200)
    context: str = Field(min_length=1, max_length=200)
    instance: Literal["testing"] = "testing"
    operation_id: str = Field(min_length=1, max_length=200)
    candidates: tuple[str, ...] = Field(default=(), max_length=1000)
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def validate_review(self) -> "NativeTestingSweepRequest":
        import re

        if len(self.candidates) != len(set(self.candidates)) or any(
            re.fullmatch(r"lp-testing-[a-f0-9]{32}", value) is None for value in self.candidates
        ):
            raise ValueError("Select distinct original-bound testing candidate namespaces.")
        if self.mode == "dry-run":
            if self.reviewed_plan_sha256:
                raise ValueError("Dry-run rejects a reviewed plan.")
        elif (
            not self.candidates or re.fullmatch(r"[a-f0-9]{64}", self.reviewed_plan_sha256) is None
        ):
            raise ValueError("Apply/readback require selected candidates and their reviewed plan.")
        return self


class NativeTestingBackingCapacity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backing_sha256: str
    free_bytes: int = Field(ge=0)
    required_additional_bytes: int = Field(ge=0)
    reserve_bytes: int = Field(ge=0)
    deficit_bytes: int = Field(ge=0)


class NativeTestingSweepResource(RetainedVolumeSweepItem):
    role: str
    backing_sha256: str = ""
    free_bytes: int | None = Field(default=None, ge=0)


class NativeTestingSweepCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    namespace: str
    operation_id: str
    product: str
    context: str
    instance: Literal["testing"] = "testing"
    target_id: str
    selected: bool
    status: Literal["eligible", "held", "removed", "absent", "unknown"]
    reasons: tuple[str, ...]
    ownership_sha256: str
    source_evidence_sha256: str = ""
    provider_evidence_sha256: str = ""
    lifecycle_snapshot_sha256: str = ""
    container_count: int = Field(default=0, ge=0)
    resources: tuple[NativeTestingSweepResource, ...] = ()
    physical_readback: dict[str, dict[str, int | str]] = Field(default_factory=dict)


class NativeTestingSweepResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: dict[str, str] = Field(default_factory=dict)
    plan_sha256: str
    candidates: tuple[NativeTestingSweepCandidate, ...]
    capacity: tuple[NativeTestingBackingCapacity, ...] = ()
    capacity_refusal: str = ""
    replayed: bool = False
