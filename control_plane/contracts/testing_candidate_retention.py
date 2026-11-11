from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.retained_volume_sweep import RetainedVolumeSweepItem
from control_plane.copy_readback_diagnostics import ReadbackPredicate


class NativeTestingSweepRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["native-testing"]
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    product: str = Field(min_length=1, max_length=200)
    context: str = Field(min_length=1, max_length=200)
    instance: Literal["testing"] = "testing"
    operation_id: str = Field(min_length=1, max_length=200)
    receiver_operation_id: str | None = None
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
    readback: dict[ReadbackPredicate, Literal["pass", "fail", "unavailable", "skipped"]] = Field(
        default_factory=dict,
        exclude_if=lambda value: not value,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class NativeTestingSweepReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str
    kind: str
    record_id: str = ""
    record_sha256: str = ""


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
    readback: dict[ReadbackPredicate, Literal["pass", "fail", "unavailable", "skipped"]] = Field(
        default_factory=dict,
        exclude_if=lambda value: not value,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    references: tuple[NativeTestingSweepReference, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


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
