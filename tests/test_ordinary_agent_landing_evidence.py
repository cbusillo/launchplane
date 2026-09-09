"""Landing review checks the combined candidate, separately from the PR delta."""

import unittest
import json

from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentCommitIdentity,
    OrdinaryAgentLandingEvidence,
    OrdinaryAgentProtectionEvidence,
    OrdinaryAgentProviderRequestCounts,
)
from control_plane.ordinary_agent_landing_evidence import (
    OrdinaryAgentLandingTechnicalCheckClient,
)
from control_plane.tenant_admission_controller import TenantAdmissionTechnicalChecks
from tests.test_owner_acceptance import _repository_evidence


class OrdinaryAgentLandingEvidenceTests(unittest.TestCase):
    def test_candidate_checks_remain_distinct_from_source_pr_evidence(self) -> None:
        repository = _repository_evidence()
        assert repository.base is not None
        target = repository.target
        candidate_sha = "f" * 40
        checks = TenantAdmissionTechnicalChecks(
            head_sha=candidate_sha,
            base_sha=repository.base.base_sha,
            strict=False,
            status="unavailable",
            evaluated_at="2026-09-09T17:00:00Z",
        )
        evidence = OrdinaryAgentLandingEvidence(
            repository_id=int(target.repository_id),
            repository_owner_id=int(target.repository_owner_id),
            repository=target.repository,
            base_ref=repository.base.base_ref,
            base_identity=OrdinaryAgentCommitIdentity(
                sha=repository.base.base_sha, tree_sha="e" * 40
            ),
            repository_evidence=repository,
            candidate_sha=candidate_sha,
            technical_checks=checks,
            protection=OrdinaryAgentProtectionEvidence(
                source="evaluated_rules",
                evaluated_rules_sha256="a" * 64,
                required_checks=(),
            ),
            expected_merge_tree_sha="d" * 40,
            observed_at=1788973200,
            counts=OrdinaryAgentProviderRequestCounts(
                rest_core_requests=0, graphql_requests=0, graphql_points=0
            ),
            evidence_sha256="b" * 64,
        )
        result = OrdinaryAgentLandingTechnicalCheckClient(evidence).read_technical_checks(
            repository=target.repository,
            base_branch=repository.base.base_ref,
            base_sha=repository.base.base_sha,
            head_sha=candidate_sha,
            evaluated_at=checks.evaluated_at,
        )
        self.assertEqual(result.head_sha, candidate_sha)
        self.assertNotEqual(result.head_sha, evidence.repository_evidence.target.head_sha)
        payload = evidence.model_dump(mode="json")
        payload["technical_checks"]["head_sha"] = target.head_sha
        payload["technical_checks"]["binding_sha256"] = ""
        with self.assertRaisesRegex(ValueError, "internally consistent"):
            OrdinaryAgentLandingEvidence.model_validate_json(json.dumps(payload))
