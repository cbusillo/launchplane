from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.change_impact import ChangeImpactTargetReference
from control_plane.contracts.governance_projection import GovernanceMergeReadinessFacet
from control_plane.contracts.merge_admission_record import MergeLandingOutcomeRecord
from control_plane.contracts.merge_admission_record import build_merge_effect_attempt_id
from control_plane.governance_projection import build_governance_projection
from tests.test_merge_readiness import _merge_admission
from tests.test_merge_readiness import _evaluate as _merge_readiness
from tests.test_merge_readiness import _owner_decision as _merge_owner_decision
from tests.test_merge_readiness import _owner_product as _merge_owner_product
from tests.test_merge_readiness import _target as _merge_target
from tests.test_merge_readiness import _candidate as _merge_candidate
from tests.test_merge_readiness import _engineering_decision as _merge_engineering_decision
from tests.test_merge_readiness import _engineering_evidence as _merge_engineering_evidence
from tests.test_merge_readiness import _fence as _merge_fence
from tests.test_owner_acceptance import (
    REPOSITORY,
    _EvidenceProvider,
    _human,
    _repository_evidence,
    _store,
)
from control_plane.owner_acceptance import record_owner_acceptance_event


TARGET = ChangeImpactTargetReference(repository=REPOSITORY, pull_request_number=2022)
NOW = "2026-08-12T05:00:00Z"


def _not_active_readiness(**_: object) -> GovernanceMergeReadinessFacet:
    return GovernanceMergeReadinessFacet(
        availability="not_active",
        reason_code="no_active_merge_lineage",
    )


class GovernanceProjectionTests(unittest.TestCase):
    def test_scenario_25_owner_accepted_remains_product_judgment_only(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            initial = build_governance_projection(
                store=store,
                repository_evidence_provider=provider,
                current_readiness_provider=_not_active_readiness,
                target=TARGET,
                base_branch="main",
                generated_at=NOW,
            )
            binding = initial.owner_judgment.current.binding
            assert binding is not None
            record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=TARGET,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=binding.binding_sha256,
                source_event_kind="browser_api",
                source_event_id="governance-scenario-25",
                occurred_at=NOW,
            )

            projection = build_governance_projection(
                store=store,
                repository_evidence_provider=provider,
                current_readiness_provider=_not_active_readiness,
                target=TARGET,
                base_branch="main",
                generated_at=NOW,
            )

        self.assertEqual(projection.owner_judgment.current.status, "accepted")
        self.assertEqual(
            projection.owner_judgment.current.human_action_semantics,
            "product_review_accepted",
        )
        self.assertEqual(projection.owner_judgment.authorizes, ())
        self.assertEqual(projection.owner_judgment.current.authorizes, ())
        self.assertEqual(projection.owner_judgment.history[0].authorizes, ())
        self.assertFalse(projection.merge_admission.admitted)
        self.assertEqual(projection.landing_outcome.status, "not_observed")

    def test_scenario_15_revocation_does_not_rewrite_admission_or_landing(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory))
            provider = _EvidenceProvider(_repository_evidence())
            initial = build_governance_projection(
                store=store,
                repository_evidence_provider=provider,
                current_readiness_provider=_not_active_readiness,
                target=TARGET,
                base_branch="main",
                generated_at=NOW,
            )
            binding = initial.owner_judgment.current.binding
            assert binding is not None
            accepted = record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=TARGET,
                identity=_human(),
                action="accepted",
                expected_binding_sha256=binding.binding_sha256,
                source_event_kind="browser_api",
                source_event_id="governance-accepted",
                occurred_at="2026-08-12T04:00:00Z",
            )
            admission_readiness = _merge_readiness(
                target=_merge_target(
                    repository=REPOSITORY,
                    pull_request_number=2022,
                    pull_request_head_sha=accepted.record.binding.head_sha,
                    pull_request_tree_sha=accepted.record.binding.tree_sha,
                ),
                owner_decision=_merge_owner_decision(
                    _merge_owner_product(
                        head_sha=accepted.record.binding.head_sha,
                        tree_sha=accepted.record.binding.tree_sha,
                    )
                ),
                candidate_evidence=_merge_candidate(
                    repository=REPOSITORY,
                    pull_request_number=2022,
                    pull_request_head_sha=accepted.record.binding.head_sha,
                ),
                engineering_decision=_merge_engineering_decision(
                    head_sha=accepted.record.binding.head_sha,
                    tree_sha=accepted.record.binding.tree_sha,
                ),
                engineering_evidence=_merge_engineering_evidence(
                    head_sha=accepted.record.binding.head_sha,
                    tree_sha=accepted.record.binding.tree_sha,
                ),
                fence_evidence=_merge_fence(
                    controller_key="merge-train-controller:example:web:main",
                    controller_repository=REPOSITORY,
                ),
            )
            attempt_id = build_merge_effect_attempt_id(
                controller_key=admission_readiness.fence.evidence.controller_key,
                lease_owner=admission_readiness.fence.evidence.observed_lease_owner,
                lease_acquired_at="2026-08-11T03:00:00Z",
                landing_plan_id="landing-plan-1",
                pull_request_number=2022,
                queue_position=2,
                attempt_sequence=1,
                expected_effect_sha=admission_readiness.target.expected_effect_sha,
            )
            admission = _merge_admission(
                attempt_id=attempt_id,
                repository=REPOSITORY,
                pull_request_number=2022,
                pull_request_head_sha=accepted.record.binding.head_sha,
                pull_request_head_tree_sha=accepted.record.binding.tree_sha,
                controller_key=admission_readiness.fence.evidence.controller_key,
                readiness=admission_readiness,
            )
            store.create_merge_admission_record_if_absent(admission)
            outcome = MergeLandingOutcomeRecord(
                admission_id=admission.admission_id,
                admission_binding_sha256=admission.admission_binding_sha256,
                attempt_id=admission.attempt_id,
                observation_sequence=1,
                source="test:scenario-15",
                repository=REPOSITORY,
                base_branch="main",
                pull_request_number=2022,
                status="landed",
                reason="provider_and_git_confirmed",
                provider_effect_attempted=True,
                observed_pull_request_state="merged",
                observed_pull_request_head_sha=admission.pull_request_head_sha,
                observed_pull_request_head_tree_sha=admission.pull_request_head_tree_sha,
                observed_base_sha=admission.candidate_sha,
                observed_base_tree_sha=admission.candidate_tree_sha,
                merge_commit_sha=admission.candidate_sha,
                merge_commit_tree_sha=admission.candidate_tree_sha,
                base_contains_merge_commit=True,
                exact_landing_confirmed=True,
                observed_at="2026-08-12T04:10:00Z",
            )
            store.create_merge_landing_outcome_record_if_absent(outcome)
            record_owner_acceptance_event(
                store=store,
                repository_evidence_provider=provider,
                target=TARGET,
                identity=_human(),
                action="revoked",
                expected_binding_sha256=binding.binding_sha256,
                source_event_kind="browser_api",
                source_event_id="governance-revoked",
                reason="Owner withdrew the product judgment after landing.",
                occurred_at=NOW,
            )

            projection = build_governance_projection(
                store=store,
                repository_evidence_provider=provider,
                current_readiness_provider=_not_active_readiness,
                target=TARGET,
                base_branch="main",
                generated_at=NOW,
            )

        self.assertEqual(projection.owner_judgment.current.status, "revoked")
        self.assertEqual(len(projection.owner_judgment.history), 2)
        self.assertTrue(projection.merge_admission.admitted)
        self.assertEqual(
            projection.merge_admission.authorizes,
            ("one_exact_merge_attempt",),
        )
        self.assertEqual(projection.landing_outcome.status, "landed")
        self.assertTrue(projection.landing_outcome.landed)


if __name__ == "__main__":
    unittest.main()
