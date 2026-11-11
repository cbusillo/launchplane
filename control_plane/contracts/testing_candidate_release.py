from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.contracts.testing_candidate_retention import (
    NativeTestingSweepCandidate,
    NativeTestingBackingCapacity,
)


TESTING_CANDIDATE_RELEASE_ROUTE = (
    "/v1/products/{product}/testing-rebuild/{operation_id}/candidate-release"
)

TestingCandidateReleaseStatus = Literal["eligible", "absent", "released", "held"]
REASON_CODE_MAX_LENGTH = 200
AUTOMATIC_RELEASE_REASONS_MAX = 32


class TestingCandidateReleaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    reviewed_plan_sha256: str = ""
    receiver_operation_id: str | None = Field(default=None, min_length=1, max_length=200)

    @model_validator(mode="after")
    def validate_review(self) -> "TestingCandidateReleaseRequest":
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        if self.mode in {"apply", "readback"} and len(self.reviewed_plan_sha256) != 64:
            raise ValueError("Apply and readback require the reviewed release plan digest.")
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Dry-run rejects a reviewed plan digest.")
        return self


class TestingCandidateReleasePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    operation_id: str
    error_code: str
    namespace: str
    resources_binding_sha256: str
    retention_plan_sha256: str = ""

    def digest(self) -> str:
        return nonvalidating_compact_sorted_json_sha256(
            self.model_dump(
                mode="json",
                exclude={"retention_plan_sha256"} if not self.retention_plan_sha256 else set(),
            )
        )


class TestingCandidateReleaseResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    product: str
    operation_id: str
    error_code: str
    plan_sha256: str
    status: TestingCandidateReleaseStatus
    reclaimable_bytes: int | None = Field(
        default=None, json_schema_extra={"x-launchplane-optional-response": True}
    )
    replayed: bool | None = Field(
        default=None, json_schema_extra={"x-launchplane-optional-response": True}
    )

    candidates: tuple[NativeTestingSweepCandidate, ...] = Field(
        default=(), json_schema_extra={"x-launchplane-optional-response": True}
    )
    capacity: tuple[NativeTestingBackingCapacity, ...] = Field(
        default=(), json_schema_extra={"x-launchplane-optional-response": True}
    )
    capacity_refusal: str = Field(
        default="", json_schema_extra={"x-launchplane-optional-response": True}
    )
    reason_code: str | None = Field(
        default=None,
        min_length=1,
        max_length=REASON_CODE_MAX_LENGTH,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class AutomaticCandidateReleaseView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["released", "absent", "held", "unknown"]
    reasons: tuple[str, ...] = Field(default=(), max_length=AUTOMATIC_RELEASE_REASONS_MAX)
    plan_sha256: str = ""
    idempotency_key: str
