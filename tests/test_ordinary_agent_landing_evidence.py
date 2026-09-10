"""Landing review checks the combined candidate, separately from the PR delta."""

import unittest
import json

from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentCommitIdentity,
    OrdinaryAgentLandingEvidence,
    OrdinaryAgentProtectionEvidence,
    OrdinaryAgentProviderRequestCounts,
)
from control_plane.ordinary_agent_github_transport import OrdinaryAgentProviderDeferred
from control_plane.ordinary_agent_landing_evidence import (
    OrdinaryAgentLandingTechnicalCheckClient,
    OrdinaryAgentLandingRepositoryEvidenceProvider,
    OrdinaryAgentLandingEvidenceMismatch,
)
from control_plane.contracts.change_impact import ChangeImpactTargetReference
from control_plane.tenant_admission_controller import TenantAdmissionTechnicalChecks
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from tests.test_owner_acceptance import _repository_evidence


class OrdinaryAgentLandingEvidenceTests(unittest.TestCase):
    def evidence(self) -> OrdinaryAgentLandingEvidence:
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
        return OrdinaryAgentLandingEvidence(
            repository_id=int(target.repository_id),
            repository_owner_id=int(target.repository_owner_id),
            repository=target.repository,
            base_ref=repository.base.base_ref,
            base_identity=OrdinaryAgentCommitIdentity(
                sha=repository.base.base_sha, tree_sha="e" * 40
            ),
            repository_evidence=repository,
            candidate_entry_evidence=(repository,),
            snapshot=MergeTrainDryRunSnapshot(
                repository=target.repository,
                base_branch=repository.base.base_ref,
                base_sha=repository.base.base_sha,
                pull_requests=(
                    MergeTrainPullRequestSnapshot(
                        number=target.pull_request_number,
                        head_sha=target.head_sha,
                        created_at=checks.evaluated_at,
                    ),
                ),
            ),
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

    def test_candidate_checks_remain_distinct_from_source_pr_evidence(self) -> None:
        evidence = self.evidence()
        target = evidence.repository_evidence.target
        checks = evidence.technical_checks
        result = OrdinaryAgentLandingTechnicalCheckClient(evidence).read_technical_checks(
            repository=target.repository,
            base_branch=evidence.base_ref,
            base_sha=evidence.base_identity.sha,
            head_sha=evidence.candidate_sha,
            evaluated_at=checks.evaluated_at,
        )
        self.assertEqual(result.head_sha, evidence.candidate_sha)
        self.assertNotEqual(result.head_sha, evidence.repository_evidence.target.head_sha)
        payload = evidence.model_dump(mode="json")
        payload["technical_checks"]["head_sha"] = target.head_sha
        payload["technical_checks"]["binding_sha256"] = ""
        with self.assertRaisesRegex(ValueError, "internally consistent"):
            OrdinaryAgentLandingEvidence.model_validate_json(json.dumps(payload))

    def test_elapsed_admission_time_preserves_fresh_checks_but_rejects_stale_or_future_evidence(
        self,
    ) -> None:
        evidence = self.evidence()
        client = OrdinaryAgentLandingTechnicalCheckClient(evidence)

        def read(at: str) -> TenantAdmissionTechnicalChecks:
            return client.read_technical_checks(
                repository=evidence.repository,
                base_branch=evidence.base_ref,
                base_sha=evidence.base_identity.sha,
                head_sha=evidence.candidate_sha,
                evaluated_at=at,
            )

        self.assertIs(read("2026-09-09T17:00:05.125Z"), evidence.technical_checks)
        with self.assertRaises(OrdinaryAgentProviderDeferred):
            read("2026-09-09T17:00:46Z")
        with self.assertRaises(OrdinaryAgentLandingEvidenceMismatch):
            read("2026-09-09T16:59:59Z")

    def test_previous_entry_resolves_without_putting_it_back_in_the_queue(self) -> None:
        evidence = self.evidence()
        current = evidence.repository_evidence
        previous = current.model_copy(
            update={
                "target": current.target.model_copy(
                    update={"pull_request_number": current.target.pull_request_number + 1}
                )
            }
        )
        payload = evidence.model_dump(mode="json")
        payload["candidate_entry_evidence"] = [
            previous.model_dump(mode="json"),
            current.model_dump(mode="json"),
        ]
        observed = OrdinaryAgentLandingEvidence.model_validate(payload)
        provider = OrdinaryAgentLandingRepositoryEvidenceProvider(observed)
        self.assertEqual(
            provider.resolve(
                ChangeImpactTargetReference(
                    repository=previous.target.repository,
                    pull_request_number=previous.target.pull_request_number,
                )
            ),
            previous,
        )
        self.assertEqual(observed.snapshot, evidence.snapshot)
        with self.assertRaises(OrdinaryAgentLandingEvidenceMismatch):
            provider.resolve(
                ChangeImpactTargetReference(
                    repository=previous.target.repository,
                    pull_request_number=previous.target.pull_request_number + 1,
                )
            )

    def test_inconsistent_queue_or_duplicate_entries_cannot_feed_evaluation(self) -> None:
        evidence = self.evidence()
        for case in ("queue_head", "duplicate", "absent_landing_target"):
            with self.subTest(case=case):
                payload = evidence.model_dump(mode="json")
                if case == "queue_head":
                    payload["snapshot"]["pull_requests"][0]["head_sha"] = "0" * 40
                elif case == "absent_landing_target":
                    payload["snapshot"]["pull_requests"] = []
                else:
                    payload["candidate_entry_evidence"].append(payload["repository_evidence"])
                with self.assertRaisesRegex(ValueError, "queue identities must agree"):
                    OrdinaryAgentLandingEvidence.model_validate(payload)
