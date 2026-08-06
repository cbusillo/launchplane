from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.change_impact import (
    ChangeImpactDecisionStatus,
    ChangeImpactReviewTier,
    ChangeImpactTarget,
    ChangeImpactTargetReference,
)


EngineeringReviewDecisionStatus = Literal[
    "approved", "changes_requested", "blocked", "pending", "unknown", "stale"
]

ENGINEERING_REVIEW_DECISION_CREATE_ACTION = "engineering_review_decision.create"
ENGINEERING_REVIEW_DECISION_READ_ACTION = "engineering_review_decision.read"
ENGINEERING_REVIEW_DECISION_PROJECT_ACTION = "engineering_review_decision.project"
ENGINEERING_REVIEW_STATUS_CONTEXT = "launchplane/engineering-review-shadow"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class EngineeringReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    target: ChangeImpactTargetReference
    work_request_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_request(self) -> "EngineeringReviewDecisionRequest":
        if self.schema_version != 1:
            raise ValueError("Unsupported engineering review decision request schema version.")
        if self.work_request_id.strip() != self.work_request_id:
            raise ValueError("Engineering review work_request_id must be canonical.")
        return self


class EngineeringReviewDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    decision_id: str = ""
    decision_binding_sha256: str = ""
    mode: Literal["shadow"] = "shadow"
    authoritative: Literal[False] = False
    enforcement_effect: Literal["none"] = "none"
    status: EngineeringReviewDecisionStatus
    reason_code: str
    target: ChangeImpactTarget
    work_request_id: str
    work_request_lifecycle_id: str
    change_impact_status: ChangeImpactDecisionStatus
    change_impact_policy_record_id: str = ""
    change_impact_policy_revision: int | None = Field(default=None, ge=1)
    change_impact_policy_digest: str = ""
    engineering_review_tier: ChangeImpactReviewTier
    required_review_count: Literal[1, 2]
    authority_id: str = ""
    authority_digest: str = ""
    authority_policy_revision: int | None = Field(default=None, ge=1)
    qualifying_run_ids: tuple[str, ...] = ()
    qualifying_model_families: tuple[str, ...] = ()
    evaluated_at: str

    @model_validator(mode="after")
    def _validate_record(self) -> "EngineeringReviewDecisionRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported engineering review decision schema version.")
        if not self.reason_code or self.reason_code.strip() != self.reason_code:
            raise ValueError("Engineering review decision requires canonical reason_code.")
        if not self.work_request_id or self.work_request_id.strip() != self.work_request_id:
            raise ValueError("Engineering review decision requires canonical work_request_id.")
        if (
            not self.work_request_lifecycle_id
            or self.work_request_lifecycle_id.strip() != self.work_request_lifecycle_id
        ):
            raise ValueError(
                "Engineering review decision requires canonical work_request_lifecycle_id."
            )
        if len(self.qualifying_run_ids) != len(set(self.qualifying_run_ids)):
            raise ValueError("Engineering review decision run ids must be distinct.")
        if tuple(sorted(self.qualifying_run_ids)) != self.qualifying_run_ids:
            raise ValueError("Engineering review decision run ids must be sorted.")
        if self.status == "approved" and len(self.qualifying_run_ids) < self.required_review_count:
            raise ValueError("Approved engineering review decision lacks required runs.")
        if self.required_review_count == 2 and self.status == "approved":
            if len(set(self.qualifying_model_families)) < 2:
                raise ValueError("Sensitive approval requires model-family diversity.")
        binding = build_engineering_review_decision_binding(self)
        expected_id = f"engineering-review-decision-{binding[:24]}"
        if self.decision_binding_sha256 and self.decision_binding_sha256 != binding:
            raise ValueError("Engineering review decision binding digest does not match payload.")
        if self.decision_id and self.decision_id != expected_id:
            raise ValueError("Engineering review decision id does not match payload.")
        object.__setattr__(self, "decision_binding_sha256", binding)
        object.__setattr__(self, "decision_id", expected_id)
        return self


class EngineeringReviewDecisionProjectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    decision_id: str = Field(min_length=1, max_length=128)


class EngineeringReviewDecisionProjectionResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    status: Literal["projected", "replayed"]
    context: Literal["launchplane/engineering-review-shadow"] = (
        "launchplane/engineering-review-shadow"
    )
    state: Literal["pending", "success", "failure", "error"]
    head_sha: str


def build_engineering_review_decision_binding(
    record: EngineeringReviewDecisionRecord,
) -> str:
    payload = record.model_dump(
        mode="json",
        exclude={"decision_id", "decision_binding_sha256", "evaluated_at"},
        exclude_none=True,
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def validate_sha256(value: str, field_name: str) -> None:
    if value and not _SHA256_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest.")
