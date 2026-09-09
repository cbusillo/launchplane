"""Normalized provider evidence for ordinary merge-train reads."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from control_plane.contracts.ordinary_agent import StrictFrozenModel
from control_plane.merge_train import MergeTrainCheckStatus, MergeTrainDryRunSnapshot


Digest = str


class OrdinaryAgentProviderRequestCounts(StrictFrozenModel):
    rest_core_requests: int = Field(ge=0, le=300)
    graphql_requests: int = Field(ge=0, le=300)
    graphql_points: int = Field(ge=0, le=5_000)


class OrdinaryAgentCommitIdentity(StrictFrozenModel):
    sha: str = Field(min_length=1, max_length=64)
    tree_sha: str = Field(min_length=1, max_length=64)
    parent_shas: tuple[str, ...] = ()

    @field_validator("parent_shas", mode="before")
    @classmethod
    def normalize_parents(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class OrdinaryAgentPullRequestHeadIdentity(StrictFrozenModel):
    pull_request_number: int = Field(gt=0)
    identity: OrdinaryAgentCommitIdentity


class OrdinaryAgentRequiredCheck(StrictFrozenModel):
    context: str = Field(min_length=1, max_length=512)
    integration_id: int | None = Field(default=None, gt=0, le=2**63 - 1)


class OrdinaryAgentProtectionEvidence(StrictFrozenModel):
    source: Literal["classic", "evaluated_rules", "both"]
    classic_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    evaluated_rules_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    required_checks: tuple[OrdinaryAgentRequiredCheck, ...]

    @field_validator("required_checks", mode="before")
    @classmethod
    def normalize_checks(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_source_evidence(self) -> OrdinaryAgentProtectionEvidence:
        if self.source in {"classic", "both"} and self.classic_sha256 is None:
            raise ValueError("classic protection source requires its digest")
        if self.source == "evaluated_rules" and self.classic_sha256 is not None:
            raise ValueError("evaluated-rules-only evidence cannot carry a classic digest")
        if len({(item.context.casefold(), item.integration_id) for item in self.required_checks}) != len(
            self.required_checks
        ):
            raise ValueError("required check evidence must be unique")
        return self


class OrdinaryAgentMergeTrainSnapshotResult(StrictFrozenModel):
    snapshot: MergeTrainDryRunSnapshot
    base_identity: OrdinaryAgentCommitIdentity
    head_identities: tuple[OrdinaryAgentPullRequestHeadIdentity, ...]
    protection: OrdinaryAgentProtectionEvidence
    counts: OrdinaryAgentProviderRequestCounts
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("head_identities", mode="before")
    @classmethod
    def normalize_heads(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_bound_identities(self) -> OrdinaryAgentMergeTrainSnapshotResult:
        if self.base_identity.sha != self.snapshot.base_sha:
            raise ValueError("snapshot base identity must match its base SHA")
        expected_heads = {
            item.number: item.head_sha for item in self.snapshot.pull_requests
        }
        observed_heads = {
            item.pull_request_number: item.identity.sha for item in self.head_identities
        }
        if observed_heads != expected_heads:
            raise ValueError("snapshot head identities must exactly match its pull requests")
        return self


class OrdinaryAgentCandidateCheckResult(StrictFrozenModel):
    candidate_identity: OrdinaryAgentCommitIdentity
    protection: OrdinaryAgentProtectionEvidence
    status: MergeTrainCheckStatus
    counts: OrdinaryAgentProviderRequestCounts
    observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

