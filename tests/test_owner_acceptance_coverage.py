import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.change_impact import (
    ChangeImpactAffectedProduct,
    ChangeImpactCoverage,
    ChangeImpactEvaluation,
    ChangeImpactTargetReference,
)
from control_plane.contracts.owner_acceptance import OwnerAcceptanceDecision
from control_plane.owner_acceptance import (
    OwnerAcceptanceImpactEvidence,
    evaluate_owner_acceptance,
)
from control_plane.owner_acceptance_projection import (
    _check_state,
    _summary,
    owner_acceptance_projection_sha256,
)
from tests.test_owner_acceptance import REPOSITORY, _EvidenceProvider, _repository_evidence, _store


def _decision() -> OwnerAcceptanceDecision:
    return OwnerAcceptanceDecision(
        status="not_required",
        reason_code="engineering_only",
        evaluated_at="2026-08-07T12:00:00Z",
    )


class OwnerAcceptanceCoverageTests(unittest.TestCase):
    def test_real_classification_preserves_coverage_without_extra_provider_reads(self) -> None:
        with TemporaryDirectory() as directory:
            store = _store(Path(directory), include_dependency_evidence=False)
            for path, status, count in (
                ("control_plane/service_auth.py", "not_required", 0),
                ("unmapped/file.py", "unavailable", 1),
            ):
                with self.subTest(path=path):
                    provider = _EvidenceProvider(_repository_evidence(path=path))
                    with patch.object(provider, "resolve", wraps=provider.resolve) as resolve:
                        decision = evaluate_owner_acceptance(
                            store=store,
                            repository_evidence_provider=provider,
                            target=ChangeImpactTargetReference(
                                repository=REPOSITORY, pull_request_number=2022
                            ),
                            evaluated_at="2026-08-07T12:00:00Z",
                        )
                    resolve.assert_called_once()
                    self.assertEqual(decision.status, status)
                    assert decision.change_impact_coverage is not None
                    self.assertEqual(decision.change_impact_coverage.unmatched_path_count, count)
                    self.assertEqual(
                        OwnerAcceptanceDecision.model_validate(decision.model_dump(mode="json")),
                        decision,
                    )
                    self.assertEqual(store.list_owner_acceptance_event_records(), ())

    def test_every_evaluation_exit_preserves_existing_typed_diagnostic(self) -> None:
        repository = _repository_evidence()
        coverage = ChangeImpactCoverage(
            state="incomplete", unmatched_path_count=1, unmatched_path_samples=("missing.py",)
        )
        product = ChangeImpactAffectedProduct(
            product="example", system="example", owner_action="deploy", owner_environment="test"
        )
        impact = ChangeImpactEvaluation(
            status="success", reason_code="classified", target=repository.target, coverage=coverage
        )
        product_decision = OwnerAcceptanceDecision(
            status="pending",
            reason_code="acceptance_missing",
            evaluated_at=_decision().evaluated_at,
        )
        for changes, expected, product_evaluation in (
            ({"status": "stale_head"}, "stale", False),
            ({"status": "unknown"}, "unavailable", False),
            ({"owner_impact": "not_required"}, "not_required", False),
            ({"owner_impact": "required"}, "unavailable", False),
            (
                {"owner_impact": "required", "affected_products": (product,)},
                "pending",
                True,
            ),
        ):
            with self.subTest(changes=changes):
                with (
                    patch(
                        "control_plane.owner_acceptance._evaluate_change_impact_for_target",
                        return_value=OwnerAcceptanceImpactEvidence(
                            repository, impact.model_copy(update=changes)
                        ),
                    ) as resolve,
                    patch(
                        "control_plane.owner_acceptance._evaluate_owner_acceptance_for_impact",
                        return_value=product_decision,
                    ) as products,
                ):
                    decision = evaluate_owner_acceptance(
                        store=object(),
                        repository_evidence_provider=_EvidenceProvider(repository),
                        target=ChangeImpactTargetReference(
                            repository=REPOSITORY, pull_request_number=2022
                        ),
                        evaluated_at="2026-08-07T12:00:00Z",
                    )
                resolve.assert_called_once()
                self.assertEqual(products.call_count, int(product_evaluation))
                self.assertEqual(decision.status, expected)
                self.assertIs(decision.change_impact_coverage, coverage)

    def test_legacy_projection_identity_and_coverage_only_refresh(self) -> None:
        original = _decision()
        self.assertEqual(
            owner_acceptance_projection_sha256(original),
            "7ea893723c191d2bd303637048f0d70d4987a7baf5693816f90eb1ab79179ce9",
        )
        legacy = original.model_dump(exclude={"change_impact_coverage"})
        self.assertEqual(OwnerAcceptanceDecision.model_validate(legacy), original)
        complete = original.model_copy(
            update={
                "change_impact_coverage": ChangeImpactCoverage(
                    state="complete", unmatched_path_count=0
                )
            }
        )
        self.assertNotEqual(
            owner_acceptance_projection_sha256(complete),
            owner_acceptance_projection_sha256(original),
        )
        self.assertEqual(
            owner_acceptance_projection_sha256(complete),
            owner_acceptance_projection_sha256(
                OwnerAcceptanceDecision.model_validate(complete.model_dump())
            ),
        )
        self.assertEqual(_check_state(original), _check_state(complete))

    def test_summary_distinguishes_coverage_and_escapes_branch_controlled_samples(self) -> None:
        hostile = ("`|</details>\n@someone", "nul\x00tab\t", "\u0301" * 256)
        incomplete = ChangeImpactCoverage(
            state="incomplete",
            unmatched_path_count=100,
            unmatched_path_samples=hostile,
            truncated=True,
        )
        for coverage, phrase in (
            (None, "coverage is unavailable"),
            (
                ChangeImpactCoverage(state="complete", unmatched_path_count=0),
                "coverage is complete",
            ),
            (incomplete, "100 unmatched paths"),
        ):
            with self.subTest(phrase=phrase):
                decision = _decision().model_copy(update={"change_impact_coverage": coverage})
                summary = _summary(decision)
                self.assertIn(phrase, summary)
                self.assertIn("does not change this check's conclusion", summary)
                self.assertEqual(_check_state(decision), _check_state(_decision()))
                if coverage is incomplete:
                    for path in hostile:
                        self.assertIn(
                            "\n    " + json.dumps(path, ensure_ascii=True) + "\n", summary
                        )
                    self.assertIn("Path samples are truncated", summary)
                    self.assertNotIn("\x00", summary)
                    self.assertNotIn("\ud800", summary)

        # Validation rejects lone surrogates, but trusted model-copy callers
        # can bypass it; rendering must still produce valid Unicode output.
        copied = incomplete.model_copy(update={"unmatched_path_samples": ("surrogate\ud800",)})
        summary = _summary(_decision().model_copy(update={"change_impact_coverage": copied}))
        self.assertIn('    "surrogate\\ud800"', summary)
        summary.encode("utf-8")

    def test_escaped_sample_growth_is_bounded_without_losing_total_count(self) -> None:
        decision = _decision().model_copy(
            update={
                "change_impact_coverage": ChangeImpactCoverage(
                    state="incomplete",
                    unmatched_path_count=20,
                    unmatched_path_samples=tuple(
                        "\U0001f642" * 254 + f"{index:02}" for index in range(20)
                    ),
                )
            }
        )
        summary = _summary(decision)
        self.assertLess(len(summary), 10_000)
        self.assertIn("20 unmatched paths", summary)
        self.assertIn("Path samples are truncated", summary)
