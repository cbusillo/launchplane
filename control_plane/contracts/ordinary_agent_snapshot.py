"""Normalized provider evidence for ordinary merge-train reads."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from control_plane.contracts.ordinary_agent import StrictFrozenModel
from control_plane.merge_train import MergeTrainCheckStatus, MergeTrainDryRunSnapshot
from control_plane.contracts.change_impact import ChangeImpactRepositoryEvidence
from control_plane.tenant_admission_controller import TenantAdmissionTechnicalChecks


Digest = str
MAX_ORDINARY_LANDING_ENTRIES = 4


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
        if len(
            {(item.context.casefold(), item.integration_id) for item in self.required_checks}
        ) != len(self.required_checks):
            raise ValueError("required check evidence must be unique")
        return self


class OrdinaryAgentMergeTrainSnapshotResult(StrictFrozenModel):
    snapshot: MergeTrainDryRunSnapshot
    base_identity: OrdinaryAgentCommitIdentity
    head_identities: tuple[OrdinaryAgentPullRequestHeadIdentity, ...]
    protection: OrdinaryAgentProtectionEvidence
    counts: OrdinaryAgentProviderRequestCounts
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def awaits_source_observation(self) -> bool:
        return any(
            item.mergeable == "unknown" or item.required_checks_status in {"pending", "unknown"}
            for item in self.snapshot.pull_requests
        )

    @field_validator("head_identities", mode="before")
    @classmethod
    def normalize_heads(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_bound_identities(self) -> OrdinaryAgentMergeTrainSnapshotResult:
        if self.base_identity.sha != self.snapshot.base_sha:
            raise ValueError("snapshot base identity must match its base SHA")
        expected_heads = {item.number: item.head_sha for item in self.snapshot.pull_requests}
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


class OrdinaryAgentLandingEvidence(StrictFrozenModel):
    """Complete normalized provider evidence for one prepared merge landing."""

    repository_id: int = Field(gt=0, le=2**63 - 1)
    repository_owner_id: int = Field(gt=0, le=2**63 - 1)
    repository: str = Field(min_length=3, max_length=512)
    base_ref: str = Field(min_length=1, max_length=255)
    base_identity: OrdinaryAgentCommitIdentity
    repository_evidence: ChangeImpactRepositoryEvidence
    candidate_entry_evidence: tuple[ChangeImpactRepositoryEvidence, ...] = Field(
        min_length=1, max_length=MAX_ORDINARY_LANDING_ENTRIES
    )
    snapshot: MergeTrainDryRunSnapshot
    candidate_sha: str = Field(min_length=1, max_length=64)
    technical_checks: TenantAdmissionTechnicalChecks
    protection: OrdinaryAgentProtectionEvidence
    expected_merge_tree_sha: str = Field(min_length=1, max_length=64)
    observed_at: int = Field(ge=0)
    counts: OrdinaryAgentProviderRequestCounts
    evidence_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("candidate_entry_evidence", mode="before")
    @classmethod
    def normalize_entries(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_exact_target(self) -> OrdinaryAgentLandingEvidence:
        target = self.repository_evidence.target
        if (
            target.repository_id != str(self.repository_id)
            or target.repository_owner_id != str(self.repository_owner_id)
            or target.repository != self.repository
            or self.repository_evidence.base is None
            or self.repository_evidence.base.base_ref != self.base_ref
            or self.repository_evidence.base.base_sha != self.base_identity.sha
            or self.technical_checks.head_sha != self.candidate_sha
            or self.technical_checks.base_sha != self.base_identity.sha
            or {(item.context, item.integration_id) for item in self.protection.required_checks}
            != {(item.name, item.app_id) for item in self.technical_checks.required_checks}
        ):
            raise ValueError("landing evidence identities must be exact and internally consistent")
        entries = {item.target.pull_request_number: item for item in self.candidate_entry_evidence}
        if (
            len(entries) != len(self.candidate_entry_evidence)
            or entries.get(target.pull_request_number) != self.repository_evidence
            or any(
                item.target.repository != self.repository
                or item.target.repository_id != str(self.repository_id)
                or item.target.repository_owner_id != str(self.repository_owner_id)
                for item in self.candidate_entry_evidence
            )
            or self.snapshot.repository != self.repository
            or self.snapshot.base_branch != self.base_ref
            or self.snapshot.base_sha != self.base_identity.sha
            or target.pull_request_number
            not in {item.number for item in self.snapshot.pull_requests}
            or len({item.number for item in self.snapshot.pull_requests})
            != len(self.snapshot.pull_requests)
            or any(
                item.number not in entries or item.head_sha != entries[item.number].target.head_sha
                for item in self.snapshot.pull_requests
            )
        ):
            raise ValueError("landing candidate evidence and queue identities must agree")
        return self
