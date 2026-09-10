"""Transport-free adapters for a custody-bound ordinary landing evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from control_plane.contracts.change_impact import (
    ChangeImpactRepositoryEvidence,
    ChangeImpactTargetReference,
)
from control_plane.contracts.ordinary_agent_snapshot import OrdinaryAgentLandingEvidence
from control_plane.contracts.ordinary_agent_effect import LANDING_EVIDENCE_MAX_AGE_SECONDS
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.ordinary_agent_github_transport import OrdinaryAgentProviderDeferred
from control_plane.tenant_admission_controller import TenantAdmissionTechnicalChecks


class OrdinaryAgentLandingEvidenceMismatch(ValueError):
    pass


@dataclass(frozen=True)
class OrdinaryAgentLandingSnapshotReader:
    repository: str
    base_branch: str
    snapshot: MergeTrainDryRunSnapshot

    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainDryRunSnapshot:
        if repository != self.repository or base_branch != self.base_branch:
            raise OrdinaryAgentLandingEvidenceMismatch("landing_snapshot_lookup_mismatch")
        return self.snapshot


@dataclass(frozen=True)
class OrdinaryAgentLandingRepositoryEvidenceProvider:
    evidence: OrdinaryAgentLandingEvidence

    def resolve(self, target: ChangeImpactTargetReference) -> ChangeImpactRepositoryEvidence:
        if target.repository == self.evidence.repository:
            for entry in self.evidence.candidate_entry_evidence:
                if entry.target.pull_request_number == target.pull_request_number:
                    return entry
        raise OrdinaryAgentLandingEvidenceMismatch("landing_repository_lookup_mismatch")


@dataclass(frozen=True)
class OrdinaryAgentLandingTechnicalCheckClient:
    evidence: OrdinaryAgentLandingEvidence

    def read_technical_checks(
        self,
        *,
        repository: str,
        base_branch: str,
        base_sha: str,
        head_sha: str,
        evaluated_at: str,
    ) -> TenantAdmissionTechnicalChecks:
        checks = self.evidence.technical_checks
        try:
            evaluation_time = datetime.fromisoformat(evaluated_at.replace("Z", "+00:00"))
            observation_time = datetime.fromisoformat(checks.evaluated_at.replace("Z", "+00:00"))
            if evaluation_time.tzinfo is None or observation_time.tzinfo is None:
                raise ValueError("landing evaluation requires timezone-aware evidence")
            evidence_age = evaluation_time.timestamp() - self.evidence.observed_at
            checks_age = (evaluation_time - observation_time).total_seconds()
        except ValueError as error:
            raise OrdinaryAgentLandingEvidenceMismatch(
                "landing_technical_lookup_mismatch"
            ) from error
        if (
            repository != self.evidence.repository
            or base_branch != self.evidence.base_ref
            or base_sha != self.evidence.base_identity.sha
            or head_sha != checks.head_sha
            or evidence_age < 0
            or checks_age < 0
        ):
            raise OrdinaryAgentLandingEvidenceMismatch("landing_technical_lookup_mismatch")
        if (
            evidence_age > LANDING_EVIDENCE_MAX_AGE_SECONDS
            or checks_age > LANDING_EVIDENCE_MAX_AGE_SECONDS
        ):
            raise OrdinaryAgentProviderDeferred()
        # Admission evaluates current authority later than the provider read.
        # Preserve the original observation and its digest instead of restamping
        # old checks as newly observed evidence.
        return checks
