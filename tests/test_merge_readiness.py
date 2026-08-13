from __future__ import annotations

import unittest

from pydantic import ValidationError

from control_plane.contracts.change_impact import ChangeImpactTarget
from control_plane.contracts.engineering_review_decision import EngineeringReviewDecisionRecord
from control_plane.contracts.engineering_review_run import EngineeringReviewRunRecord
from control_plane.contracts.merge_admission_record import (
    MergeAdmissionRecord,
    MergeLandingOutcomeRecord,
    build_merge_effect_attempt_id,
    validate_merge_landing_outcome_for_admission,
)
from control_plane.contracts.merge_readiness import (
    MERGE_READINESS_POLICY_DIMENSIONS,
    MergeReadinessAdvisoryObservation,
    MergeReadinessCandidateEvidence,
    MergeReadinessEngineeringEvidenceReference,
    MergeReadinessFenceEvidence,
    MergeReadinessPolicyFingerprintEvidence,
    MergeReadinessPolicyFingerprints,
    MergeReadinessResult,
    MergeReadinessTarget,
    MergeReadinessTechnicalCheckEvidence,
)
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
    build_merge_train_controller_key,
)
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainStructuralCandidateResult,
)
from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceBinding,
    OwnerAcceptanceDecision,
    OwnerAcceptanceProductDecision,
)
from control_plane.merge_readiness import (
    evaluate_merge_readiness,
    evaluate_merge_readiness_from_live_evidence,
)
from control_plane.tenant_admission_controller import (
    TenantAdmissionRequiredTechnicalCheck,
    TenantAdmissionTechnicalCheckSignal,
    TenantAdmissionTechnicalChecks,
)


REPOSITORY = "example/launchplane"
BASE_SHA = "1" * 40
HEAD_SHA = "2" * 40
TREE_SHA = "3" * 40
CANDIDATE_SHA = "4" * 40
OTHER_SHA = "5" * 40
POLICY_SHA = "a" * 64
OTHER_POLICY_SHA = "b" * 64
EVIDENCE_SHA = "c" * 64
EVALUATED_AT = "2026-08-11T03:00:00Z"


def _target(**updates: object) -> MergeReadinessTarget:
    payload: dict[str, object] = {
        "repository": REPOSITORY,
        "pull_request_number": 2083,
        "base_branch": "main",
        "base_sha": BASE_SHA,
        "pull_request_head_sha": HEAD_SHA,
        "pull_request_tree_sha": TREE_SHA,
        "expected_effect_sha": CANDIDATE_SHA,
        "queue_position": 2,
    }
    payload.update(updates)
    return MergeReadinessTarget.model_validate(payload)


def _owner_binding(
    *,
    product: str = "alpha",
    system: str = "web",
    head_sha: str = HEAD_SHA,
    tree_sha: str = TREE_SHA,
) -> OwnerAcceptanceBinding:
    return OwnerAcceptanceBinding(
        repository_id="101",
        repository_owner_id="202",
        repository=REPOSITORY,
        pull_request_number=2083,
        head_sha=head_sha,
        tree_sha=tree_sha,
        change_impact_policy_record_id="impact-policy-1",
        change_impact_policy_revision=1,
        change_impact_policy_digest=POLICY_SHA,
        product=product,
        system=system,
        action="change",
        environment="production",
        owner_policy_record_id="owner-policy-1",
        owner_policy_revision=1,
        owner_policy_digest=POLICY_SHA,
        owner_requirement_record_id="owner-requirement-1",
        owner_requirement_revision=1,
        owner_requirement_digest=POLICY_SHA,
    )


def _owner_product(
    *,
    product: str = "alpha",
    system: str = "web",
    status: str = "accepted",
    reason_code: str = "acceptance_valid",
    admissible: bool = True,
    head_sha: str = HEAD_SHA,
    tree_sha: str = TREE_SHA,
) -> OwnerAcceptanceProductDecision:
    binding = None
    if status != "not_required":
        binding = _owner_binding(
            product=product,
            system=system,
            head_sha=head_sha,
            tree_sha=tree_sha,
        )
    return OwnerAcceptanceProductDecision.model_validate(
        {
            "product": product,
            "system": system,
            "action": "change",
            "environment": "production",
            "status": status,
            "reason_code": reason_code,
            "binding": binding.model_dump(mode="json") if binding is not None else None,
            "admissible": admissible,
        }
    )


def _owner_decision(
    *products: OwnerAcceptanceProductDecision,
) -> OwnerAcceptanceDecision:
    first = products[0]
    return OwnerAcceptanceDecision(
        status=first.status,
        reason_code=first.reason_code,
        binding=first.binding,
        admissible=all(product.admissible for product in products)
        and all(product.status == "accepted" for product in products),
        products=products,
        evaluated_at=EVALUATED_AT,
    )


def _engineering_decision(
    *,
    status: str = "approved",
    head_sha: str = HEAD_SHA,
    tree_sha: str = TREE_SHA,
    qualifying_run_ids: tuple[str, ...] | None = None,
) -> EngineeringReviewDecisionRecord:
    run_ids = ("review-run-1",) if qualifying_run_ids is None else qualifying_run_ids
    if status != "approved" and qualifying_run_ids is None:
        run_ids = ()
    return EngineeringReviewDecisionRecord.model_validate(
        {
            "status": status,
            "reason_code": {
                "approved": "required_reviews_approved",
                "pending": "insufficient_completed_reviews",
                "changes_requested": "changes_requested",
                "blocked": "review_blocked",
                "unknown": "review_authority_unavailable",
                "stale": "review_authority_stale",
            }[status],
            "target": ChangeImpactTarget(
                repository_id="101",
                repository_owner_id="202",
                repository=REPOSITORY,
                pull_request_number=2083,
                head_sha=head_sha,
                tree_sha=tree_sha,
            ).model_dump(mode="json"),
            "work_request_id": "work-request-2083",
            "work_request_lifecycle_id": "work-request-lifecycle-2083",
            "change_impact_status": "success",
            "change_impact_policy_record_id": "impact-policy-1",
            "change_impact_policy_revision": 1,
            "change_impact_policy_digest": POLICY_SHA,
            "engineering_review_tier": "routine",
            "required_review_count": 1,
            "qualifying_run_ids": run_ids,
            "qualifying_model_families": ("openai",) if run_ids else (),
            "evaluated_at": EVALUATED_AT,
        }
    )


def _engineering_evidence(
    *,
    head_sha: str = HEAD_SHA,
    tree_sha: str = TREE_SHA,
) -> tuple[MergeReadinessEngineeringEvidenceReference, ...]:
    return (
        MergeReadinessEngineeringEvidenceReference(
            run_id="review-run-1",
            head_sha=head_sha,
            tree_sha=tree_sha,
            evidence_digest=EVIDENCE_SHA,
        ),
    )


def _policy_fingerprints(
    *,
    missing: str = "",
    drift: str = "",
) -> MergeReadinessPolicyFingerprints:
    payload: dict[str, object] = {}
    for dimension in MERGE_READINESS_POLICY_DIMENSIONS:
        current_sha256: str | None = POLICY_SHA
        if dimension == missing:
            current_sha256 = None
        elif dimension == drift:
            current_sha256 = OTHER_POLICY_SHA
        payload[dimension] = MergeReadinessPolicyFingerprintEvidence(
            dimension=dimension,
            expected_sha256=POLICY_SHA,
            current_sha256=current_sha256,
        )
    return MergeReadinessPolicyFingerprints.model_validate(payload)


def _all_missing_policy_fingerprints() -> MergeReadinessPolicyFingerprints:
    return MergeReadinessPolicyFingerprints.model_validate(
        {
            dimension: MergeReadinessPolicyFingerprintEvidence(
                dimension=dimension,
                expected_sha256=POLICY_SHA,
                current_sha256=None,
            )
            for dimension in MERGE_READINESS_POLICY_DIMENSIONS
        }
    )


def _candidate(**updates: object) -> MergeReadinessCandidateEvidence:
    payload: dict[str, object] = {
        "structural_status": "exact",
        "record_id": "candidate-record-1",
        "repository": REPOSITORY,
        "base_sha": BASE_SHA,
        "candidate_sha": CANDIDATE_SHA,
        "pull_request_number": 2083,
        "queue_position": 2,
        "pull_request_head_sha": HEAD_SHA,
    }
    payload.update(updates)
    return MergeReadinessCandidateEvidence.model_validate(payload)


def _fence(**updates: object) -> MergeReadinessFenceEvidence:
    payload: dict[str, object] = {
        "controller_key": "merge-train-controller:example:launchplane:main",
        "controller_repository": REPOSITORY,
        "controller_base_branch": "main",
        "controller_status": "running",
        "expected_lease_owner": "controller-run-1",
        "observed_lease_owner": "controller-run-1",
        "lease_expires_at": "2026-08-11T03:10:00Z",
        "observed_effect_sha": CANDIDATE_SHA,
    }
    payload.update(updates)
    return MergeReadinessFenceEvidence.model_validate(payload)


def _checks(
    *,
    status: str = "pass",
    head_sha: str = CANDIDATE_SHA,
    advisories: tuple[MergeReadinessAdvisoryObservation, ...] = (),
) -> MergeReadinessTechnicalCheckEvidence:
    return MergeReadinessTechnicalCheckEvidence.model_validate(
        {
            "head_sha": head_sha,
            "status": status,
            "required_checks": ("ci-gate", "security-gate"),
            "advisory_observations": advisories,
        }
    )


def _evaluate(**updates: object) -> MergeReadinessResult:
    payload: dict[str, object] = {
        "target": _target(),
        "owner_decision": _owner_decision(_owner_product()),
        "technical_checks": _checks(),
        "engineering_decision": _engineering_decision(),
        "engineering_evidence": _engineering_evidence(),
        "policy_fingerprints": _policy_fingerprints(),
        "candidate_evidence": _candidate(),
        "fence_evidence": _fence(),
        "evaluated_at": EVALUATED_AT,
    }
    payload.update(updates)
    return evaluate_merge_readiness(**payload)  # type: ignore[arg-type]


def _merge_admission(**updates: object) -> MergeAdmissionRecord:
    readiness = _evaluate()
    attempt_id = build_merge_effect_attempt_id(
        controller_key=readiness.fence.evidence.controller_key,
        lease_owner=readiness.fence.evidence.observed_lease_owner,
        lease_acquired_at="2026-08-11T03:00:00Z",
        landing_plan_id="landing-plan-1",
        pull_request_number=2083,
        queue_position=2,
        attempt_sequence=1,
        expected_effect_sha=CANDIDATE_SHA,
    )
    payload: dict[str, object] = {
        "attempt_id": attempt_id,
        "attempt_sequence": 1,
        "source": "test:merge-admission",
        "repository": REPOSITORY,
        "base_branch": "main",
        "pull_request_number": 2083,
        "queue_position": 2,
        "batch_id": "batch-1",
        "candidate_record_id": "candidate-record-1",
        "landing_plan_record_id": "landing-record-1",
        "landing_plan_id": "landing-plan-1",
        "merge_method": "merge",
        "effective_base_sha": BASE_SHA,
        "effective_base_tree_sha": OTHER_SHA,
        "pull_request_head_sha": HEAD_SHA,
        "pull_request_head_tree_sha": TREE_SHA,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree_sha": "6" * 40,
        "expected_effect_sha": CANDIDATE_SHA,
        "candidate_sha256": POLICY_SHA,
        "structural_provenance_sha256": EVIDENCE_SHA,
        "landing_plan_sha256": OTHER_POLICY_SHA,
        "readiness": readiness,
        "structural_result": MergeTrainStructuralCandidateResult(
            status="exact",
            reason_codes=("structural_single_entry_exact",),
            effective_base_sha=BASE_SHA,
            effective_base_tree_sha=OTHER_SHA,
            candidate_sha256=POLICY_SHA,
            landing_plan_sha256=OTHER_POLICY_SHA,
            provenance_sha256=EVIDENCE_SHA,
        ),
        "admission_algorithm_version": "merge-admission-v1",
        "controller_key": readiness.fence.evidence.controller_key,
        "lease_owner": readiness.fence.evidence.observed_lease_owner,
        "lease_acquired_at": "2026-08-11T03:00:00Z",
        "lease_expires_at": readiness.fence.evidence.lease_expires_at,
        "created_at": "2026-08-11T03:01:00Z",
    }
    payload.update(updates)
    return MergeAdmissionRecord.model_validate(payload)


class MergeReadinessScenarioTests(unittest.TestCase):
    def test_scenario_1_owner_acceptance_remains_historical_while_checks_pending(self) -> None:
        owner_decision = _owner_decision(_owner_product())
        owner_payload = owner_decision.model_dump(mode="json")

        result = _evaluate(
            owner_decision=owner_decision,
            technical_checks=_checks(status="pending"),
        )

        self.assertEqual(result.state, "blocked_checks")
        self.assertEqual(result.owner_facets[0].state, "ready")
        self.assertIn("checks_pending", result.reason_codes)
        self.assertEqual(owner_decision.model_dump(mode="json"), owner_payload)
        self.assertEqual(result.authorizes, ())

    def test_scenario_2_exact_evidence_becomes_ready_without_authority(self) -> None:
        result = _evaluate()

        self.assertEqual(result.state, "ready")
        self.assertFalse(result.authoritative)
        self.assertEqual(result.mode, "ephemeral")
        self.assertEqual(result.authorizes, ())

    def test_engineering_only_owner_decision_without_products_is_ready(self) -> None:
        owner_decision = OwnerAcceptanceDecision(
            status="not_required",
            reason_code="engineering_only",
            evaluated_at=EVALUATED_AT,
        )

        result = _evaluate(owner_decision=owner_decision)

        self.assertEqual(result.state, "ready")
        self.assertEqual(len(result.owner_facets), 1)
        self.assertEqual(result.owner_facets[0].product, "__not_applicable__")
        self.assertEqual(result.owner_facets[0].state, "ready")
        self.assertEqual(result.owner_facets[0].reason_codes, ("owner_not_required",))

    def test_unavailable_owner_decision_without_products_fails_closed(self) -> None:
        owner_decision = OwnerAcceptanceDecision(
            status="unavailable",
            reason_code="change_impact_unavailable",
            evaluated_at=EVALUATED_AT,
        )

        result = _evaluate(owner_decision=owner_decision)

        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.owner_facets[0].state, "unknown")
        self.assertIn("owner_evidence_unavailable", result.reason_codes)

    def test_scenario_3_failed_or_unknown_checks_cannot_be_ready(self) -> None:
        cases = (
            ("fail", "blocked_checks", "checks_failed"),
            ("unknown", "unknown", "checks_unknown"),
        )
        for check_status, expected_state, reason in cases:
            with self.subTest(check_status=check_status):
                result = _evaluate(technical_checks=_checks(status=check_status))
                self.assertEqual(result.state, expected_state)
                self.assertIn(reason, result.reason_codes)
                self.assertEqual(result.authorizes, ())

    def test_owner_not_required_is_an_independent_ready_product_facet(self) -> None:
        not_required = _owner_product(
            product="docs",
            system="documentation",
            status="not_required",
            reason_code="engineering_only",
            admissible=False,
        )

        result = _evaluate(owner_decision=_owner_decision(not_required))

        self.assertEqual(result.state, "ready")
        self.assertEqual(result.owner_facets[0].state, "ready")
        self.assertEqual(result.owner_facets[0].reason_codes, ("owner_not_required",))

    def test_scenario_7_authority_age_and_self_review_fail_closed(self) -> None:
        cases = (
            ("unavailable", "owner_authority_denied", "owner_authority_denied"),
            ("stale", "owner_review_expired", "owner_review_expired"),
            ("unavailable", "self_review_denied", "owner_self_review_denied"),
            (
                "unavailable",
                "contributing_identity_unknown",
                "owner_contributing_identity_unknown",
            ),
        )
        for owner_status, owner_reason, expected_reason in cases:
            with self.subTest(owner_reason=owner_reason):
                owner = _owner_product(
                    status=owner_status,
                    reason_code=owner_reason,
                    admissible=False,
                )
                result = _evaluate(owner_decision=_owner_decision(owner))
                self.assertEqual(result.state, "blocked_owner_evidence")
                self.assertIn(expected_reason, result.reason_codes)

    def test_scenario_20_preview_isolation_history_remains_inadmissible(self) -> None:
        owner = _owner_product(
            status="stale",
            reason_code="preview_isolation_insufficient",
            admissible=False,
        )
        owner_payload = owner.model_dump(mode="json")

        result = _evaluate(owner_decision=_owner_decision(owner))

        self.assertEqual(result.state, "blocked_owner_evidence")
        self.assertIn("owner_preview_isolation_insufficient", result.reason_codes)
        self.assertEqual(owner.model_dump(mode="json"), owner_payload)
        self.assertEqual(result.authorizes, ())

    def test_scenario_8_policy_or_unrelated_base_drift_replans(self) -> None:
        policy_result = _evaluate(policy_fingerprints=_policy_fingerprints(drift="merge_train"))
        base_result = _evaluate(candidate_evidence=_candidate(base_sha=OTHER_SHA))

        self.assertEqual(policy_result.state, "blocked_policy")
        self.assertIn("policy_merge_train_drift", policy_result.reason_codes)
        self.assertEqual(base_result.state, "blocked_candidate_identity")
        self.assertIn("candidate_base_mismatch", base_result.reason_codes)

    def test_recorded_rolling_candidate_allows_recorded_base_advancement(self) -> None:
        result = _evaluate(
            candidate_evidence=_candidate(
                structural_status="recorded_rolling",
                base_sha=OTHER_SHA,
            )
        )

        self.assertEqual(result.state, "ready")
        self.assertIn("candidate_recorded_rolling", result.reason_codes)
        self.assertNotIn("candidate_base_mismatch", result.reason_codes)

    def test_scenario_12_every_live_dimension_revalidates_between_entries(self) -> None:
        cases = (
            (
                {
                    "owner_decision": _owner_decision(
                        _owner_product(
                            status="unavailable",
                            reason_code="owner_authority_unavailable",
                            admissible=False,
                        )
                    )
                },
                "blocked_owner_evidence",
                "owner_authority_unavailable",
            ),
            (
                {"technical_checks": _checks(status="fail")},
                "blocked_checks",
                "checks_failed",
            ),
            (
                {"candidate_evidence": _candidate(pull_request_head_sha=OTHER_SHA)},
                "blocked_candidate_identity",
                "candidate_head_mismatch",
            ),
            (
                {"candidate_evidence": _candidate(queue_position=3)},
                "blocked_candidate_identity",
                "candidate_queue_mismatch",
            ),
            (
                {"policy_fingerprints": _policy_fingerprints(drift="authorization")},
                "blocked_policy",
                "policy_authorization_drift",
            ),
        )
        for updates, expected_state, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                result = _evaluate(**updates)
                self.assertEqual(result.state, expected_state)
                self.assertIn(expected_reason, result.reason_codes)

    def test_scenario_22_lease_or_expected_sha_loss_blocks_effect(self) -> None:
        cases = (
            (
                _fence(observed_lease_owner="another-controller"),
                "controller_lease_lost",
            ),
            (
                _fence(lease_expires_at="2026-08-11T03:00:00Z"),
                "controller_lease_expired",
            ),
            (_fence(observed_effect_sha=OTHER_SHA), "expected_sha_mismatch"),
            (
                _fence(controller_repository="other/repository"),
                "controller_scope_mismatch",
            ),
        )
        for fence, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                result = _evaluate(fence_evidence=fence)
                self.assertEqual(result.state, "blocked_candidate_identity")
                self.assertIn(expected_reason, result.reason_codes)
                self.assertEqual(result.authorizes, ())

    def test_scenario_24_mixed_products_retain_each_facet_and_worst_state(self) -> None:
        products = (
            _owner_product(product="ready-product"),
            _owner_product(
                product="not-required-product",
                system="docs",
                status="not_required",
                reason_code="engineering_only",
                admissible=False,
            ),
            _owner_product(
                product="blocked-product",
                status="changes_requested",
                reason_code="changes_requested",
                admissible=False,
            ),
        )

        result = _evaluate(owner_decision=_owner_decision(*reversed(products)))

        self.assertEqual(result.state, "blocked_owner_evidence")
        self.assertEqual(
            tuple(facet.product for facet in result.owner_facets),
            ("blocked-product", "not-required-product", "ready-product"),
        )
        self.assertIn("owner_changes_requested", result.reason_codes)
        self.assertIn("owner_not_required", result.reason_codes)
        self.assertIn("owner_acceptance_valid", result.reason_codes)


class MergeReadinessFacetTests(unittest.TestCase):
    def test_owner_head_and_tree_drift_remain_distinguishable(self) -> None:
        result = _evaluate(
            owner_decision=_owner_decision(_owner_product(head_sha=OTHER_SHA, tree_sha=OTHER_SHA))
        )

        self.assertEqual(result.state, "blocked_owner_evidence")
        self.assertIn("owner_evidence_head_mismatch", result.reason_codes)
        self.assertIn("owner_evidence_tree_mismatch", result.reason_codes)

    def test_checks_pending_fail_unknown_and_exact_head_are_independent(self) -> None:
        result = _evaluate(technical_checks=_checks(status="unknown", head_sha=OTHER_SHA))

        self.assertEqual(result.state, "unknown")
        self.assertIn("checks_unknown", result.reason_codes)
        self.assertIn("checks_head_mismatch", result.reason_codes)

    def test_engineering_outcomes_and_head_drift_are_distinguishable(self) -> None:
        cases = (
            ("pending", "blocked_engineering_review", "engineering_review_pending"),
            (
                "changes_requested",
                "blocked_engineering_review",
                "engineering_review_changes_requested",
            ),
            ("blocked", "blocked_engineering_review", "engineering_review_blocked"),
            ("stale", "blocked_engineering_review", "engineering_review_stale"),
            ("unknown", "unknown", "engineering_review_unknown"),
        )
        for status, expected_state, expected_reason in cases:
            with self.subTest(status=status):
                result = _evaluate(
                    engineering_decision=_engineering_decision(status=status),
                    engineering_evidence=(),
                )
                self.assertEqual(result.state, expected_state)
                self.assertIn(expected_reason, result.reason_codes)

        drifted = _evaluate(
            engineering_decision=_engineering_decision(head_sha=OTHER_SHA),
            engineering_evidence=_engineering_evidence(),
        )
        self.assertEqual(drifted.state, "blocked_engineering_review")
        self.assertIn("engineering_review_head_mismatch", drifted.reason_codes)

    def test_approved_engineering_review_requires_exact_qualifying_evidence(self) -> None:
        missing = _evaluate(engineering_evidence=())
        drifted = _evaluate(engineering_evidence=_engineering_evidence(head_sha=OTHER_SHA))

        self.assertEqual(missing.state, "unknown")
        self.assertIn("engineering_review_evidence_missing", missing.reason_codes)
        self.assertEqual(drifted.state, "unknown")
        self.assertIn("engineering_review_evidence_missing", drifted.reason_codes)

    def test_advisory_engineering_review_remains_visible_without_blocking(self) -> None:
        result = _evaluate(
            engineering_decision=None,
            engineering_evidence=(),
            engineering_review_authority="advisory",
            policy_fingerprints=_policy_fingerprints(missing="engineering_review"),
        )

        self.assertEqual(result.state, "ready")
        self.assertEqual(result.engineering_review.state, "unknown")
        self.assertEqual(result.policy.state, "ready")
        self.assertIn("engineering_review_evidence_missing", result.reason_codes)
        self.assertIn("policy_engineering_review_missing", result.reason_codes)
        self.assertNotIn("policy_fingerprints_match", result.reason_codes)

    def test_advisory_failed_engineering_review_remains_non_blocking(self) -> None:
        result = _evaluate(
            engineering_decision=_engineering_decision(status="changes_requested"),
            engineering_evidence=(),
            engineering_review_authority="advisory",
        )

        self.assertEqual(result.state, "ready")
        self.assertEqual(result.engineering_review.state, "blocked_engineering_review")
        self.assertIn("engineering_review_changes_requested", result.reason_codes)

    def test_advisory_engineering_review_does_not_hide_other_policy_drift(self) -> None:
        result = _evaluate(
            engineering_decision=None,
            engineering_evidence=(),
            engineering_review_authority="advisory",
            policy_fingerprints=_policy_fingerprints(drift="ruleset"),
        )

        self.assertEqual(result.state, "blocked_policy")
        self.assertEqual(result.engineering_review.state, "unknown")
        self.assertIn("policy_ruleset_drift", result.reason_codes)
        self.assertNotIn("policy_fingerprints_match", result.reason_codes)

    def test_required_engineering_review_still_fails_closed(self) -> None:
        result = _evaluate(
            engineering_decision=None,
            engineering_evidence=(),
            engineering_review_authority="required",
            policy_fingerprints=_policy_fingerprints(missing="engineering_review"),
        )

        self.assertEqual(result.state, "unknown")
        self.assertEqual(result.engineering_review.state, "unknown")
        self.assertEqual(result.policy.state, "blocked_policy")

    def test_legacy_required_readiness_payload_preserves_digest(self) -> None:
        result = _evaluate()
        payload = result.model_dump(mode="json")
        payload.pop("engineering_review_authority")

        restored = MergeReadinessResult.model_validate(payload)

        self.assertEqual(restored.engineering_review_authority, "required")
        self.assertEqual(restored.readiness_digest, result.readiness_digest)

    def test_every_policy_dimension_fails_closed_when_missing_or_drifted(self) -> None:
        for dimension in MERGE_READINESS_POLICY_DIMENSIONS:
            with self.subTest(dimension=dimension, condition="missing"):
                result = _evaluate(policy_fingerprints=_policy_fingerprints(missing=dimension))
                self.assertEqual(result.state, "blocked_policy")
                self.assertIn(f"policy_{dimension}_missing", result.reason_codes)
            with self.subTest(dimension=dimension, condition="drift"):
                result = _evaluate(policy_fingerprints=_policy_fingerprints(drift=dimension))
                self.assertEqual(result.state, "blocked_policy")
                self.assertIn(f"policy_{dimension}_drift", result.reason_codes)

    def test_all_policy_dimensions_missing_retain_all_reasons(self) -> None:
        result = _evaluate(policy_fingerprints=_all_missing_policy_fingerprints())

        self.assertEqual(result.state, "blocked_policy")
        for dimension in MERGE_READINESS_POLICY_DIMENSIONS:
            self.assertIn(f"policy_{dimension}_missing", result.reason_codes)

    def test_candidate_structural_queue_base_and_head_states_are_distinct(self) -> None:
        cases = (
            (
                _candidate(structural_status="mismatch"),
                "candidate_identity_mismatch",
            ),
            (_candidate(structural_status="unknown"), "candidate_identity_unknown"),
            (_candidate(queue_position=1), "candidate_queue_mismatch"),
            (_candidate(base_sha=OTHER_SHA), "candidate_base_mismatch"),
            (_candidate(pull_request_head_sha=OTHER_SHA), "candidate_head_mismatch"),
            (_candidate(candidate_sha=OTHER_SHA), "candidate_identity_mismatch"),
        )
        for candidate, expected_reason in cases:
            with self.subTest(expected_reason=expected_reason):
                result = _evaluate(candidate_evidence=candidate)
                self.assertNotEqual(result.state, "ready")
                self.assertIn(expected_reason, result.reason_codes)

    def test_unknown_is_never_ready_even_when_every_other_facet_passes(self) -> None:
        result = _evaluate(candidate_evidence=_candidate(structural_status="unknown"))

        self.assertEqual(result.state, "unknown")
        self.assertNotEqual(result.state, "ready")


class MergeReadinessDeterminismTests(unittest.TestCase):
    def test_advisory_observations_do_not_change_state_or_digest(self) -> None:
        baseline = _evaluate()
        observed = _evaluate(
            technical_checks=_checks(
                advisories=(
                    MergeReadinessAdvisoryObservation(
                        name="launchplane/owner-acceptance",
                        state="fail",
                    ),
                    MergeReadinessAdvisoryObservation(
                        name="launchplane/engineering-review",
                        state="pending",
                    ),
                )
            )
        )

        self.assertEqual(observed.state, baseline.state)
        self.assertEqual(observed.reason_codes, baseline.reason_codes)
        self.assertEqual(observed.readiness_digest, baseline.readiness_digest)
        self.assertEqual(len(observed.technical_checks.advisory_observations), 2)

    def test_evaluated_at_is_excluded_from_readiness_digest(self) -> None:
        first = _evaluate(evaluated_at="2026-08-11T03:00:00Z")
        second = _evaluate(evaluated_at="2026-08-11T03:01:00Z")

        self.assertNotEqual(first.evaluated_at, second.evaluated_at)
        self.assertEqual(first.readiness_digest, second.readiness_digest)

    def test_product_order_and_worst_state_ties_are_deterministic(self) -> None:
        alpha = _owner_product(
            product="alpha",
            status="changes_requested",
            reason_code="changes_requested",
            admissible=False,
        )
        beta = _owner_product(
            product="beta",
            status="revoked",
            reason_code="acceptance_revoked",
            admissible=False,
        )

        first = _evaluate(owner_decision=_owner_decision(alpha, beta))
        second = _evaluate(owner_decision=_owner_decision(beta, alpha))

        self.assertEqual(first.state, "blocked_owner_evidence")
        self.assertEqual(first.owner_facets, second.owner_facets)
        self.assertEqual(first.reason_codes, second.reason_codes)
        self.assertEqual(first.readiness_digest, second.readiness_digest)


class MergeReadinessLiveAdapterTests(unittest.TestCase):
    def test_adapter_consumes_existing_evidence_without_writes_or_advisory_authority(
        self,
    ) -> None:
        target = _target()
        technical_checks = TenantAdmissionTechnicalChecks(
            head_sha=CANDIDATE_SHA,
            base_sha=BASE_SHA,
            strict=False,
            status="fail",
            required_checks=(
                TenantAdmissionRequiredTechnicalCheck(name="ci-gate"),
                TenantAdmissionRequiredTechnicalCheck(name="launchplane/owner-acceptance"),
            ),
            signals=(
                TenantAdmissionTechnicalCheckSignal(
                    source="check_run",
                    name="ci-gate",
                    state="pass",
                ),
                TenantAdmissionTechnicalCheckSignal(
                    source="check_run",
                    name="launchplane/owner-acceptance",
                    state="fail",
                ),
            ),
            evaluated_at=EVALUATED_AT,
        )
        candidate_record = MergeTrainBatchCandidateRecord(
            record_id="candidate-record-1",
            source="merge-train-controller",
            updated_at=EVALUATED_AT,
            candidate=MergeTrainBatchCandidate(
                batch_id="batch-1",
                repository=REPOSITORY,
                base_branch="main",
                base_sha=BASE_SHA,
                policy_key="default",
                policy_sha256=POLICY_SHA,
                candidate_ref="refs/heads/launchplane/candidate-1",
                candidate_sha=CANDIDATE_SHA,
                status="passed",
                entries=(
                    MergeTrainBatchEntry(
                        pull_request_number=2082,
                        position=1,
                        head_sha=OTHER_SHA,
                    ),
                    MergeTrainBatchEntry(
                        pull_request_number=2083,
                        position=2,
                        head_sha=HEAD_SHA,
                    ),
                ),
                required_checks_status="pass",
                created_at=EVALUATED_AT,
                updated_at=EVALUATED_AT,
            ),
        )
        controller_state = MergeTrainControllerStateRecord(
            controller_key=build_merge_train_controller_key(
                repository=REPOSITORY,
                base_branch="main",
            ),
            repository=REPOSITORY,
            base_branch="main",
            policy_key="default",
            policy_sha256=POLICY_SHA,
            status="running",
            updated_at=EVALUATED_AT,
            lease_owner="controller-run-1",
            lease_acquired_at="2026-08-11T02:59:00Z",
            lease_expires_at="2026-08-11T03:10:00Z",
            heartbeat_at="2026-08-11T03:00:00Z",
        )
        engineering_run = EngineeringReviewRunRecord.model_construct(
            run_id="review-run-1",
            head_sha=HEAD_SHA,
            tree_sha=TREE_SHA,
            state="completed",
            evidence_digest=EVIDENCE_SHA,
        )

        result = evaluate_merge_readiness_from_live_evidence(
            target=target,
            owner_decision=_owner_decision(_owner_product()),
            engineering_decision=_engineering_decision(),
            engineering_runs=(engineering_run,),
            technical_checks=technical_checks,
            policy_fingerprints=_policy_fingerprints(),
            candidate_record=candidate_record,
            structural_candidate_status="exact",
            controller_state=controller_state,
            expected_lease_owner="controller-run-1",
            observed_effect_sha=CANDIDATE_SHA,
            evaluated_at=EVALUATED_AT,
        )

        self.assertEqual(result.state, "ready")
        self.assertEqual(result.technical_checks.required_checks, ("ci-gate",))
        self.assertEqual(len(result.technical_checks.advisory_observations), 1)
        self.assertNotIn("checks_failed", result.reason_codes)
        self.assertEqual(result.authorizes, ())

    def test_missing_live_evidence_returns_unknown_without_authority(self) -> None:
        result = evaluate_merge_readiness_from_live_evidence(
            target=_target(),
            owner_decision=OwnerAcceptanceDecision(
                status="not_required",
                reason_code="engineering_only",
                evaluated_at=EVALUATED_AT,
            ),
            engineering_decision=None,
            engineering_runs=(),
            technical_checks=None,
            policy_fingerprints=_all_missing_policy_fingerprints(),
            candidate_record=None,
            structural_candidate_status="unknown",
            controller_state=None,
            expected_lease_owner="controller-run-1",
            observed_effect_sha="",
            evaluated_at=EVALUATED_AT,
        )

        self.assertEqual(result.state, "unknown")
        self.assertFalse(result.authoritative)
        self.assertEqual(result.authorizes, ())
        self.assertEqual(result.technical_checks.state, "unknown")
        self.assertEqual(result.engineering_review.state, "unknown")
        self.assertEqual(result.candidate.state, "unknown")
        self.assertEqual(result.fence.state, "blocked_candidate_identity")


class MergeReadinessModelValidationTests(unittest.TestCase):
    def test_contract_is_frozen_and_forbids_extra_fields(self) -> None:
        result = _evaluate()

        with self.assertRaises(ValidationError):
            result.state = "blocked_checks"
        payload = result.model_dump(mode="json")
        payload["unexpected"] = True
        with self.assertRaises(ValidationError):
            MergeReadinessResult.model_validate(payload)


class MergeAdmissionRecordTests(unittest.TestCase):
    def test_ready_evidence_builds_deterministic_admission(self) -> None:
        first = _merge_admission()
        second = _merge_admission()

        self.assertEqual(first, second)
        self.assertTrue(first.admission_id.startswith("merge-admission-"))
        self.assertEqual(len(first.admission_binding_sha256), 64)
        self.assertEqual(first.readiness.state, "ready")

    def test_admission_contract_rejects_non_deterministic_attempt_id(self) -> None:
        admission = _merge_admission()
        payload = admission.model_dump(mode="json")
        payload.update(
            {
                "admission_id": "",
                "admission_binding_sha256": "",
                "attempt_id": "merge-effect-attempt-not-deterministic",
            }
        )

        with self.assertRaisesRegex(ValidationError, "deterministic identity"):
            MergeAdmissionRecord.model_validate(payload)

    def test_non_ready_evidence_cannot_be_admitted(self) -> None:
        with self.assertRaises(ValidationError):
            _merge_admission(readiness=_evaluate(technical_checks=_checks(status="pending")))

    def test_landed_outcome_requires_exact_matching_git_evidence(self) -> None:
        admission = _merge_admission()
        outcome = MergeLandingOutcomeRecord(
            admission_id=admission.admission_id,
            admission_binding_sha256=admission.admission_binding_sha256,
            attempt_id=admission.attempt_id,
            observation_sequence=1,
            source="test:landed",
            repository=admission.repository,
            base_branch=admission.base_branch,
            pull_request_number=admission.pull_request_number,
            status="landed",
            reason="provider_and_git_confirmed",
            provider_effect_attempted=True,
            observed_pull_request_head_sha=admission.pull_request_head_sha,
            observed_pull_request_head_tree_sha=admission.pull_request_head_tree_sha,
            observed_base_sha="7" * 40,
            observed_base_tree_sha="8" * 40,
            merge_commit_sha="9" * 40,
            merge_commit_tree_sha="a" * 40,
            base_contains_merge_commit=True,
            exact_landing_confirmed=True,
            observed_at="2026-08-11T03:02:00Z",
        )

        validate_merge_landing_outcome_for_admission(
            admission=admission,
            outcome=outcome,
        )
        self.assertEqual(outcome.status, "landed")

    def test_ambiguous_transport_is_never_rejected(self) -> None:
        admission = _merge_admission()
        outcome = MergeLandingOutcomeRecord(
            admission_id=admission.admission_id,
            admission_binding_sha256=admission.admission_binding_sha256,
            attempt_id=admission.attempt_id,
            observation_sequence=1,
            source="test:ambiguous",
            repository=admission.repository,
            base_branch=admission.base_branch,
            pull_request_number=admission.pull_request_number,
            status="reconcile_required",
            reason="provider_transport_ambiguous",
            provider_effect_attempted=True,
            provider_message="connection reset after request send",
            observed_at="2026-08-11T03:02:00Z",
        )

        self.assertFalse(outcome.provider_conclusive_rejection)
        self.assertFalse(outcome.exact_landing_confirmed)

    def test_contract_rejects_authority_state_reason_and_digest_forgery(self) -> None:
        result = _evaluate()
        cases = (
            {"authorizes": ["merge"]},
            {"state": "blocked_checks"},
            {"reason_codes": ["checks_failed"]},
            {"readiness_digest": "f" * 64},
            {"state": "not_a_canonical_state"},
        )
        for update in cases:
            with self.subTest(update=update):
                payload = result.model_dump(mode="json")
                payload.update(update)
                with self.assertRaises(ValidationError):
                    MergeReadinessResult.model_validate(payload)

    def test_policy_contract_requires_all_seven_exact_dimensions(self) -> None:
        payload = _policy_fingerprints().model_dump(mode="json")
        payload.pop("ruleset")
        with self.assertRaises(ValidationError):
            MergeReadinessPolicyFingerprints.model_validate(payload)

        payload = _policy_fingerprints().model_dump(mode="json")
        payload["ruleset"]["dimension"] = "impact"
        with self.assertRaises(ValidationError):
            MergeReadinessPolicyFingerprints.model_validate(payload)

    def test_technical_check_contract_excludes_launchplane_advisory_authority(self) -> None:
        with self.assertRaises(ValidationError):
            MergeReadinessTechnicalCheckEvidence(
                head_sha=CANDIDATE_SHA,
                status="pass",
                required_checks=("launchplane/owner-acceptance",),
            )
        with self.assertRaises(ValidationError):
            MergeReadinessTechnicalCheckEvidence(
                head_sha=CANDIDATE_SHA,
                status="pass",
                required_checks=("ci-gate",),
                advisory_observations=(
                    MergeReadinessAdvisoryObservation(
                        name="ci-gate",
                        state="pass",
                    ),
                ),
            )

        payload = _evaluate().model_dump(mode="json")
        payload["technical_checks"]["required_checks"] = ["launchplane/owner-acceptance"]
        with self.assertRaises(ValidationError):
            MergeReadinessResult.model_validate(payload)

        payload = _evaluate().model_dump(mode="json")
        payload["technical_checks"]["advisory_observations"] = [
            {"name": "ci-gate", "state": "pass"}
        ]
        with self.assertRaises(ValidationError):
            MergeReadinessResult.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
