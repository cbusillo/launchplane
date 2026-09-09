"""Transport-free adapters for a custody-bound ordinary landing evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from control_plane.contracts.change_impact import (
    ChangeImpactRepositoryEvidence,
    ChangeImpactTargetReference,
)
from control_plane.contracts.ordinary_agent_snapshot import OrdinaryAgentLandingEvidence
from control_plane.merge_train import MergeTrainDryRunSnapshot
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
        observed = self.evidence.repository_evidence.target
        if (
            target.repository != self.evidence.repository
            or target.pull_request_number != observed.pull_request_number
        ):
            raise OrdinaryAgentLandingEvidenceMismatch("landing_repository_lookup_mismatch")
        return self.evidence.repository_evidence


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
        if (
            repository != self.evidence.repository
            or base_branch != self.evidence.base_ref
            or base_sha != self.evidence.base_identity.sha
            or head_sha != checks.head_sha
            or evaluated_at != checks.evaluated_at
        ):
            raise OrdinaryAgentLandingEvidenceMismatch("landing_technical_lookup_mismatch")
        return checks
