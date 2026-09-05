import unittest

from control_plane.change_impact_service import evaluate_change_impact
from tests.test_change_impact import _policy, _repository_evidence, _stored_evidence


class ChangeImpactCoverageTests(unittest.TestCase):
    def test_pure_gaps_are_sorted_bounded_and_still_fail_closed(self) -> None:
        paths = tuple(f"unmapped/{index:03}.py" for index in range(25))
        forward = evaluate_change_impact(
            repository_evidence=_repository_evidence(*paths),
            policies=(_policy(),),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        reversed_paths = evaluate_change_impact(
            repository_evidence=_repository_evidence(*reversed(paths)),
            policies=(_policy(),),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        self.assertEqual(forward, reversed_paths)
        self.assertEqual(forward.reason_code, "policy_coverage_incomplete")
        self.assertEqual(
            (
                forward.status,
                forward.engineering_review_tier,
                forward.required_engineering_review_count,
                forward.owner_impact,
                forward.affected_products,
            ),
            ("unknown", "sensitive", 2, "unknown", ()),
        )
        assert forward.coverage is not None
        self.assertEqual(
            forward.coverage.model_dump(),
            {
                "state": "incomplete",
                "unmatched_path_count": 25,
                "unmatched_path_samples": paths[:20],
                "truncated": True,
            },
        )
        self.assertEqual(len(forward.unknown_evidence), 21)

    def test_long_path_sample_is_bounded_without_changing_coverage_count(self) -> None:
        path = "unmapped/" + "a" * 300
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence(path),
            policies=(_policy(),),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        assert result.coverage is not None
        self.assertEqual(
            result.coverage.model_dump(),
            {
                "state": "incomplete",
                "unmatched_path_count": 1,
                "unmatched_path_samples": (path[:256],),
                "truncated": True,
            },
        )
        self.assertNotIn(path, " ".join(result.unknown_evidence))

    def test_mixed_evidence_preserves_contradiction_and_declared_product(self) -> None:
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("unmapped/file.py", "src/runtime/app.py"),
            policies=(_policy(),),
            stored_evidence=(_stored_evidence("generic-web-runtime", confidence="ambiguous"),),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        self.assertEqual(result.reason_code, "ambiguous_or_missing_evidence")
        self.assertEqual((result.status, result.owner_impact), ("unknown", "required"))
        self.assertEqual(
            tuple(item.product for item in result.affected_products), ("generic-web-a",)
        )
        assert result.coverage is not None
        self.assertEqual(
            result.coverage.model_dump(),
            {
                "state": "incomplete",
                "unmatched_path_count": 1,
                "unmatched_path_samples": ("unmapped/file.py",),
                "truncated": False,
            },
        )
        self.assertEqual(
            result.unknown_evidence,
            (
                "ambiguous stored evidence for generic-web-runtime",
                "no policy rule matched changed path unmapped/file.py",
            ),
        )

    def test_complete_path_coverage_does_not_validate_contradictory_evidence(self) -> None:
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("src/runtime/app.py"),
            policies=(_policy(),),
            stored_evidence=(_stored_evidence("generic-web-runtime", confidence="ambiguous"),),
            evaluated_at="2026-08-06T01:00:00Z",
        )
        self.assertEqual(result.status, "unknown")
        self.assertEqual(result.reason_code, "ambiguous_or_missing_evidence")
        assert result.coverage is not None
        self.assertEqual(result.coverage.state, "complete")

    def test_missing_policy_has_no_path_coverage_claim(self) -> None:
        result = evaluate_change_impact(
            repository_evidence=_repository_evidence("unmapped/file.py"),
            policies=(),
        )
        self.assertEqual(result.reason_code, "policy_unavailable")
        self.assertIsNone(result.coverage)
