from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, ValidationInfo
from pydantic import field_validator, model_validator


def _required_text(value: object, field_name: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is required")
    return value.strip()


class MergeTrainHistoricalCompletionEntrySelector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position: StrictInt = Field(gt=0)
    pull_request_number: StrictInt = Field(gt=0)
    expected_head_sha: str
    expected_head_tree_sha: str

    @field_validator("expected_head_sha", "expected_head_tree_sha", mode="before")
    @classmethod
    def _validate_text(cls, value: object, info: ValidationInfo) -> str:
        return _required_text(value, info.field_name)


class MergeTrainHistoricalCompletionSelector(BaseModel):
    """The controller binding a future completion disposition would validate."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_active_record_id: str
    expected_effect_sha: str
    expected_policy_sha256: str
    expected_landing_plan_id: str
    expected_entries: tuple[MergeTrainHistoricalCompletionEntrySelector, ...] = Field(
        min_length=1, max_length=25
    )

    @field_validator(
        "expected_active_record_id",
        "expected_effect_sha",
        "expected_policy_sha256",
        "expected_landing_plan_id",
        mode="before",
    )
    @classmethod
    def _validate_text(cls, value: object, info: ValidationInfo) -> str:
        return _required_text(value, info.field_name)

    @model_validator(mode="after")
    def _validate_entries(self) -> "MergeTrainHistoricalCompletionSelector":
        positions = tuple(entry.position for entry in self.expected_entries)
        numbers = tuple(entry.pull_request_number for entry in self.expected_entries)
        if positions != tuple(range(1, len(positions) + 1)):
            raise ValueError("expected_entries positions must be ordered without gaps")
        if len(set(numbers)) != len(numbers):
            raise ValueError("expected_entries pull requests must be unique")
        return self


class MergeTrainHistoricalCompletionEntryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pull_request_number: StrictInt = Field(gt=0)
    position: StrictInt = Field(gt=0)
    expected_head_sha: str
    expected_head_tree_sha: str
    expected_base_sha: str
    expected_parent_tree_sha: str
    expected_result_tree_sha: str
    observed_head_sha: str
    observed_head_tree_sha: str
    observed_merge_commit_sha: str
    observed_merge_commit_tree_sha: str
    observed_parent_sha: str
    observed_parent_tree_sha: str
    base_contains_merge_commit: StrictBool

    @field_validator(
        "expected_head_sha",
        "expected_head_tree_sha",
        "expected_base_sha",
        "expected_parent_tree_sha",
        "expected_result_tree_sha",
        "observed_head_sha",
        "observed_head_tree_sha",
        "observed_merge_commit_sha",
        "observed_merge_commit_tree_sha",
        "observed_parent_sha",
        "observed_parent_tree_sha",
        mode="before",
    )
    @classmethod
    def _validate_text(cls, value: object, info: ValidationInfo) -> str:
        return _required_text(value, info.field_name)


class MergeTrainHistoricalCompletionProviderEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    observed_at: str
    observed_base_sha: str
    observed_base_tree_sha: str
    final_observed_base_sha: str
    entries: tuple[MergeTrainHistoricalCompletionEntryEvidence, ...] = Field(
        min_length=1, max_length=25
    )
    provider_effect_attempted: Literal[False] = False

    @field_validator(
        "observed_at",
        "observed_base_sha",
        "observed_base_tree_sha",
        "final_observed_base_sha",
        mode="before",
    )
    @classmethod
    def _validate_text(cls, value: object, info: ValidationInfo) -> str:
        return _required_text(value, info.field_name)

    @model_validator(mode="after")
    def _validate_entries(self) -> "MergeTrainHistoricalCompletionProviderEvidence":
        positions = tuple(entry.position for entry in self.entries)
        numbers = tuple(entry.pull_request_number for entry in self.entries)
        if positions != tuple(range(1, len(positions) + 1)):
            raise ValueError("entries positions must be ordered without gaps")
        if len(set(numbers)) != len(numbers):
            raise ValueError("entries pull requests must be unique")
        return self


class MergeTrainHistoricalDispositionAuthorization(BaseModel):
    """Authority used to record the observation, not to perform the historical merge."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_scope: str
    idempotency_key: str
    recorded_at: str
    action: str
    product: str
    context: str
    authz_policy_record_id: str
    authz_policy_revision: StrictInt = Field(ge=1)
    authz_policy_sha256: str
    merge_policy_record_id: str
    merge_policy_sha256: str

    @field_validator(
        "actor_scope",
        "idempotency_key",
        "recorded_at",
        "action",
        "product",
        "context",
        "authz_policy_record_id",
        "authz_policy_sha256",
        "merge_policy_record_id",
        "merge_policy_sha256",
        mode="before",
    )
    @classmethod
    def _validate_text(cls, value: object, info: ValidationInfo) -> str:
        return _required_text(value, info.field_name)


class MergeTrainHistoricalCompletionEvidence(BaseModel):
    """Historical observation, with versioned attribution for a service disposition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 1
    classification: Literal["observed_merged_without_admission"]
    authority_state: Literal["observation_only"]
    source_landing_plan_record_id: str
    source_landing_plan_sha256: str
    controller_key: str
    repository: str
    base_branch: str
    landing_plan_id: str
    batch_id: str
    candidate_sha: str
    candidate_sha256: str
    policy_key: str
    policy_sha256: str
    trace_id: str
    provider_evidence: MergeTrainHistoricalCompletionProviderEvidence
    disposition_authorization: MergeTrainHistoricalDispositionAuthorization | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def _validate_disposition_authorization(self) -> "MergeTrainHistoricalCompletionEvidence":
        if (self.schema_version == 2) != (self.disposition_authorization is not None):
            raise ValueError("historical evidence schema2 requires disposition authorization")
        if (
            self.disposition_authorization is not None
            and self.disposition_authorization.merge_policy_sha256 != self.policy_sha256
        ):
            raise ValueError("disposition authorization must match the selected plan policy")
        return self

    @field_validator(
        "source_landing_plan_record_id",
        "source_landing_plan_sha256",
        "controller_key",
        "repository",
        "base_branch",
        "landing_plan_id",
        "batch_id",
        "candidate_sha",
        "candidate_sha256",
        "policy_key",
        "policy_sha256",
        "trace_id",
        mode="before",
    )
    @classmethod
    def _validate_text(cls, value: object, info: ValidationInfo) -> str:
        return _required_text(value, info.field_name)
