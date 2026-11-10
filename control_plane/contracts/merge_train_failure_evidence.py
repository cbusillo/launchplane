from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MergeTrainFailedRequiredCheck(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    app_id: int | None = Field(default=None, gt=0, strict=True)
    source: Literal["check_run", "commit_status"]
    provider_id: int = Field(gt=0, strict=True)
    provider_app_id: int | None = Field(default=None, gt=0, strict=True)
    provider_app_slug: str | None = None
    conclusion: str = Field(min_length=1)
    check_suite_id: int | None = Field(default=None, gt=0, strict=True)
    run_id: int | None = Field(default=None, gt=0, strict=True)
    run_attempt: int | None = Field(default=None, gt=0, strict=True)

    @model_validator(mode="after")
    def _validate_origin(self) -> MergeTrainFailedRequiredCheck:
        if self.app_id is not None and self.app_id != self.provider_app_id:
            raise ValueError("Failed check does not match its required App")
        if self.source == "commit_status":
            if self.conclusion not in {"failure", "error"} or any(
                value is not None for value in (self.check_suite_id, self.run_id, self.run_attempt)
            ):
                raise ValueError("Failed commit status origin is invalid")
        elif (
            self.check_suite_id is None
            or self.provider_app_id is None
            or not self.provider_app_slug
            or self.conclusion
            not in {
                "failure",
                "timed_out",
                "action_required",
                "cancelled",
                "stale",
                "startup_failure",
            }
            or (self.run_id is None) != (self.run_attempt is None)
            or (self.provider_app_slug == "github-actions") != (self.run_id is not None)
        ):
            raise ValueError("Failed check run origin is invalid")
        return self


class MergeTrainBlockFailureEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str = Field(min_length=1)
    candidate_ref: str = Field(min_length=1)
    candidate_sha: str = Field(min_length=1)
    checks: tuple[MergeTrainFailedRequiredCheck, ...] = Field(min_length=1)
