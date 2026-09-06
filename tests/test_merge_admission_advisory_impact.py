from types import SimpleNamespace
from typing import Literal
import unittest
from unittest.mock import patch

from control_plane.contracts.change_impact import ChangeImpactEvaluation
from control_plane.contracts.engineering_review_decision import EngineeringReviewDecisionRecord
from control_plane.contracts.merge_readiness import MergeReadinessPolicyFingerprints
from control_plane.contracts.owner_acceptance import OwnerAcceptanceBinding, OwnerAcceptanceDecision
from control_plane.merge_admission_impact_binding import select_current_engineering_decision
from control_plane.merge_admission_live import LiveMergeAdmissionEvaluator
from tests.test_merge_admission_live import (
    _PassingTechnicalCheckClient,
    _UnusedRepositoryEvidenceProvider,
)
from tests.test_merge_readiness import (
    BASE_SHA,
    EVALUATED_AT,
    HEAD_SHA,
    POLICY_SHA,
    REPOSITORY,
    _engineering_decision,
    _evaluate,
    _owner_decision,
    _owner_product,
)


def _impact(version: Literal[2] | None) -> ChangeImpactEvaluation:
    return ChangeImpactEvaluation(
        status="success",
        reason_code="classified",
        target=_engineering_decision().target,
        policy_digest=POLICY_SHA,
        binding_hash_version=version,
        change_impact_decision_digest=POLICY_SHA if version == 2 else None,
    )


def _engineering(
    version: Literal[2] | None, *, digest: str = POLICY_SHA
) -> EngineeringReviewDecisionRecord:
    payload = _engineering_decision(status="stale").model_dump(
        exclude={"decision_id", "decision_binding_sha256"}
    )
    payload.update(
        binding_hash_version=version,
        change_impact_policy_digest=digest,
        change_impact_decision_digest=digest if version == 2 else None,
        authority_digest="c" * 64,
    )
    return EngineeringReviewDecisionRecord.model_validate(payload)


def _owner(version: Literal[2] | None, *, digest: str = POLICY_SHA) -> OwnerAcceptanceDecision:
    product = _owner_product()
    assert product.binding is not None
    payload = product.binding.model_dump(exclude={"binding_sha256"})
    payload.update(
        binding_hash_version=version,
        change_impact_policy_digest=digest,
        change_impact_decision_digest=digest if version == 2 else None,
    )
    binding = OwnerAcceptanceBinding.model_validate(payload)
    return _owner_decision(product.model_copy(update={"binding": binding}))


class AdvisoryImpactAuthorityTests(unittest.TestCase):
    def _fingerprints(
        self,
        *,
        impact: ChangeImpactEvaluation,
        owner: OwnerAcceptanceDecision,
        engineering: EngineeringReviewDecisionRecord | None,
        mode: Literal["required", "advisory"] | None,
    ) -> MergeReadinessPolicyFingerprints:
        client = _PassingTechnicalCheckClient()
        evaluator = LiveMergeAdmissionEvaluator(
            store=object(),
            repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
            technical_check_client=client,
        )
        mode_kwargs = {} if mode is None else {"engineering_review_authority": mode}
        with patch(
            "control_plane.merge_admission_live.read_active_authz_policy_record",
            return_value=SimpleNamespace(policy_sha256=POLICY_SHA),
        ):
            return evaluator._policy_fingerprints(
                impact=impact,
                owner_decision=owner,
                engineering_decision=engineering,
                engineering_authority_digest=POLICY_SHA,
                technical_checks=client.read_technical_checks(
                    repository=REPOSITORY,
                    base_branch="main",
                    base_sha=BASE_SHA,
                    head_sha=HEAD_SHA,
                    evaluated_at=EVALUATED_AT,
                ),
                landing_policy_sha256=POLICY_SHA,
                current_merge_train_policy_sha256=POLICY_SHA,
                **mode_kwargs,
            )

    def test_advisory_records_never_enter_impact_authority_but_remain_diagnostic(self) -> None:
        for version in (None, 2):
            impact = _impact(version)
            for with_owner in (False, True):
                owner = (
                    _owner(version)
                    if with_owner
                    else OwnerAcceptanceDecision(
                        status="not_required",
                        reason_code="engineering_only",
                        evaluated_at=EVALUATED_AT,
                    )
                )
                for scenario in ("missing", "matching", "stale", "wrong_version"):
                    with self.subTest(version=version, owner=with_owner, scenario=scenario):
                        record = (
                            None
                            if scenario == "missing"
                            else _engineering(
                                (2 if version is None else None)
                                if scenario == "wrong_version"
                                else version,
                                digest="b" * 64 if scenario == "stale" else POLICY_SHA,
                            )
                        )
                        selected = select_current_engineering_decision(
                            impact=impact, decisions=() if record is None else (record,)
                        )
                        fingerprints = self._fingerprints(
                            impact=impact, owner=owner, engineering=selected, mode="advisory"
                        )
                        self.assertEqual(
                            (
                                fingerprints.impact.expected_sha256,
                                fingerprints.impact.current_sha256,
                            ),
                            (POLICY_SHA, POLICY_SHA),
                        )
                        result = _evaluate(
                            owner_decision=owner,
                            engineering_decision=selected,
                            engineering_evidence=(),
                            engineering_review_authority="advisory",
                            policy_fingerprints=fingerprints,
                        )
                        self.assertEqual(result.state, "ready")
                        self.assertIn("policy_engineering_review_drift", result.reason_codes)
                        if selected is not None:
                            self.assertEqual(
                                result.engineering_review.state, "blocked_engineering_review"
                            )
                            self.assertIn("engineering_review_stale", result.reason_codes)
                            self.assertEqual(
                                fingerprints.engineering_review.expected_sha256, "c" * 64
                            )

    def test_required_default_preserves_drift_and_does_not_weaken_owner_authority(self) -> None:
        for version in (None, 2):
            impact = _impact(version)
            owner = _owner(version)
            record = _engineering(version, digest="b" * 64)
            required = self._fingerprints(
                impact=impact, owner=owner, engineering=record, mode="required"
            )
            self.assertEqual(
                required,
                self._fingerprints(impact=impact, owner=owner, engineering=record, mode=None),
            )
            result = _evaluate(
                owner_decision=owner,
                engineering_decision=record,
                engineering_evidence=(),
                policy_fingerprints=required,
            )
            self.assertEqual(result.policy.state, "blocked_policy")
            self.assertNotEqual(result.state, "ready")
            for owner_evidence in (
                _owner(version, digest="b" * 64),
                OwnerAcceptanceDecision(
                    status="pending", reason_code="acceptance_missing", evaluated_at=EVALUATED_AT
                ),
            ):
                with self.subTest(version=version, owner=owner_evidence.status):
                    fingerprints = self._fingerprints(
                        impact=impact,
                        owner=owner_evidence,
                        engineering=_engineering(version),
                        mode="advisory",
                    )
                    result = _evaluate(
                        owner_decision=owner_evidence,
                        engineering_decision=record,
                        engineering_evidence=(),
                        engineering_review_authority="advisory",
                        policy_fingerprints=fingerprints,
                    )
                    self.assertEqual(result.policy.state, "blocked_policy")
                    self.assertNotEqual(result.state, "ready")
