from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from click.testing import CliRunner, Result
from pydantic import ValidationError

from control_plane.cli import main
from control_plane.contracts.dependency_health import (
    DependencyHealthFinding,
    DependencyHealthPolicy,
    DependencyHealthProvenanceMismatch,
    DependencyHealthSeverity,
    DependencyHealthSnapshot,
    DependencyHealthSnapshotProvenance,
    compare_dependency_health_snapshots,
    evaluate_dependency_health_absolute,
    evaluate_dependency_health_regression,
)

BASE_COMMIT = "a" * 40
CANDIDATE_COMMIT = "b" * 40
SCAN_CONFIGURATION_SHA256 = "c" * 64


def _finding(
    advisory_id: str,
    *,
    severity: DependencyHealthSeverity = "high",
    package: str = "example-package",
    versions: tuple[str, ...] = ("1.0.0",),
    aliases: tuple[str, ...] = (),
    manifest_path: str = "package-lock.json",
    occurrence_count: int = 1,
) -> DependencyHealthFinding:
    return DependencyHealthFinding(
        advisory_id=advisory_id,
        aliases=aliases,
        ecosystem="npm",
        package=package,
        versions=versions,
        occurrence_count=occurrence_count,
        manifest_path=manifest_path,
        severity=severity,
    )


def _snapshot(
    findings: tuple[DependencyHealthFinding, ...],
    *,
    source_commit: str = BASE_COMMIT,
    baseline_commit: str = "",
    advisory_revision: str = "revision-1",
    producer_version: str = "1.0.0",
) -> DependencyHealthSnapshot:
    return DependencyHealthSnapshot(
        generated_at="2026-07-31T13:00:00Z",
        provenance=DependencyHealthSnapshotProvenance(
            repository="example/repository",
            source_commit=source_commit,
            baseline_commit=baseline_commit,
            producer="example-scanner-adapter",
            producer_version=producer_version,
            advisory_source="example-advisory-source",
            advisory_revision=advisory_revision,
            scan_scope="production",
            scan_configuration_sha256=SCAN_CONFIGURATION_SHA256,
        ),
        findings=findings,
    )


def _candidate(
    findings: tuple[DependencyHealthFinding, ...], **kwargs: str
) -> DependencyHealthSnapshot:
    return _snapshot(
        findings,
        source_commit=CANDIDATE_COMMIT,
        baseline_commit=BASE_COMMIT,
        **kwargs,
    )


class DependencyHealthContractTests(unittest.TestCase):
    def test_snapshot_normalizes_and_sorts_findings(self) -> None:
        snapshot = _snapshot(
            (
                _finding("ghsa-bbbb", versions=("2.0.0", "1.0.0", "2.0.0")),
                _finding("cve-2026-0001", aliases=("ghsa-aaaa",)),
            )
        )

        self.assertEqual(
            [finding.advisory_id for finding in snapshot.findings],
            ["CVE-2026-0001", "GHSA-BBBB"],
        )
        self.assertEqual(snapshot.findings[0].aliases, ("GHSA-AAAA",))
        self.assertEqual(snapshot.findings[1].versions, ("1.0.0", "2.0.0"))

    def test_snapshot_rejects_duplicate_finding_identity(self) -> None:
        with self.assertRaisesRegex(ValidationError, "duplicate findings"):
            _snapshot((_finding("GHSA-AAAA"), _finding("ghsa-aaaa", versions=("2",))))

    def test_finding_rejects_alias_that_repeats_canonical_id(self) -> None:
        with self.assertRaisesRegex(ValidationError, "cannot repeat advisory_id"):
            _finding("GHSA-AAAA", aliases=("ghsa-aaaa",))

    def test_contract_rejects_coerced_numeric_evidence(self) -> None:
        finding_payload = _finding("GHSA-AAAA").model_dump(mode="json")
        finding_payload["occurrence_count"] = True
        with self.assertRaises(ValidationError):
            DependencyHealthFinding.model_validate(finding_payload)

        snapshot_payload = _snapshot(()).model_dump(mode="json")
        snapshot_payload["schema_version"] = "1"
        with self.assertRaises(ValidationError):
            DependencyHealthSnapshot.model_validate(snapshot_payload)

    def test_comparison_classifies_findings_deterministically(self) -> None:
        baseline = _snapshot(
            (
                _finding("GHSA-UNCHANGED", versions=("1.0.0",)),
                _finding("GHSA-WORSENED", severity="moderate"),
                _finding("GHSA-RESOLVED"),
            )
        )
        candidate = _candidate(
            (
                _finding("GHSA-INTRODUCED", package="new-package"),
                _finding("GHSA-WORSENED", severity="high"),
                _finding("GHSA-UNCHANGED", severity="moderate", versions=("1.1.0",)),
            )
        )

        comparison = compare_dependency_health_snapshots(
            baseline=baseline,
            candidate=candidate,
        )

        self.assertEqual(
            [finding.advisory_id for finding in comparison.introduced],
            ["GHSA-INTRODUCED"],
        )
        self.assertEqual(
            [finding.candidate.advisory_id for finding in comparison.worsened],
            ["GHSA-WORSENED"],
        )
        self.assertEqual(
            [finding.advisory_id for finding in comparison.resolved],
            ["GHSA-RESOLVED"],
        )
        self.assertEqual(
            [finding.candidate.advisory_id for finding in comparison.unchanged],
            ["GHSA-UNCHANGED"],
        )

    def test_same_cardinality_version_replacement_is_non_regressing(self) -> None:
        baseline = _snapshot((_finding("GHSA-EXISTING", versions=("1.0.0",)),))
        candidate = _candidate((_finding("GHSA-EXISTING", versions=("1.0.1",)),))

        evaluation = evaluate_dependency_health_regression(
            baseline=baseline,
            candidate=candidate,
        )

        self.assertEqual(evaluation.policy_evaluation.status, "pass")
        self.assertEqual(evaluation.policy_evaluation.reason_codes, ())

    def test_new_moderate_finding_is_reported_without_blocking(self) -> None:
        evaluation = evaluate_dependency_health_regression(
            baseline=_snapshot(()),
            candidate=_candidate((_finding("GHSA-MODERATE", severity="moderate"),)),
        )

        self.assertEqual(evaluation.policy_evaluation.status, "pass")
        self.assertEqual(
            [finding.advisory_id for finding in evaluation.comparison.introduced],
            ["GHSA-MODERATE"],
        )

    def test_new_high_finding_blocks(self) -> None:
        evaluation = evaluate_dependency_health_regression(
            baseline=_snapshot(()),
            candidate=_candidate((_finding("GHSA-HIGH"),)),
        )

        self.assertEqual(evaluation.policy_evaluation.status, "fail")
        self.assertEqual(
            evaluation.policy_evaluation.reason_codes,
            ("introduced_high_or_critical",),
        )

    def test_worsened_finding_blocks_only_when_candidate_is_high_or_critical(self) -> None:
        evaluation = evaluate_dependency_health_regression(
            baseline=_snapshot((_finding("GHSA-WORSE", severity="moderate"),)),
            candidate=_candidate((_finding("GHSA-WORSE", severity="high"),)),
        )

        self.assertEqual(evaluation.policy_evaluation.status, "fail")
        self.assertEqual(
            evaluation.policy_evaluation.reason_codes,
            ("worsened_to_high_or_critical",),
        )

    def test_affected_version_or_occurrence_expansion_is_worsened(self) -> None:
        baseline = _snapshot(
            (
                _finding("GHSA-VERSIONS", versions=("1.0.0",)),
                _finding("GHSA-OCCURRENCES", package="other", occurrence_count=1),
            )
        )
        candidate = _candidate(
            (
                _finding("GHSA-VERSIONS", versions=("1.0.0", "2.0.0")),
                _finding("GHSA-OCCURRENCES", package="other", occurrence_count=2),
            )
        )

        evaluation = evaluate_dependency_health_regression(
            baseline=baseline,
            candidate=candidate,
        )

        self.assertEqual(
            [finding.candidate.advisory_id for finding in evaluation.comparison.worsened],
            ["GHSA-OCCURRENCES", "GHSA-VERSIONS"],
        )
        self.assertEqual(evaluation.policy_evaluation.status, "fail")

    def test_provenance_mismatch_fails_closed_without_values(self) -> None:
        baseline = _snapshot(())
        candidate = _candidate((), advisory_revision="revision-2", producer_version="2.0.0")

        with self.assertRaises(DependencyHealthProvenanceMismatch) as raised:
            compare_dependency_health_snapshots(
                baseline=baseline,
                candidate=candidate,
            )

        self.assertEqual(
            raised.exception.mismatched_fields,
            ("advisory_revision", "producer_version"),
        )
        self.assertNotIn("revision-2", str(raised.exception))

    def test_candidate_must_assert_exact_baseline_commit(self) -> None:
        candidate = _snapshot(
            (),
            source_commit=CANDIDATE_COMMIT,
            baseline_commit="d" * 40,
        )

        with self.assertRaises(DependencyHealthProvenanceMismatch) as raised:
            compare_dependency_health_snapshots(
                baseline=_snapshot(()),
                candidate=candidate,
            )

        self.assertEqual(
            raised.exception.mismatched_fields,
            ("candidate_baseline_commit",),
        )

    def test_target_advisories_must_exist_in_baseline_and_resolve(self) -> None:
        policy = DependencyHealthPolicy(target_advisory_ids=("CVE-2026-0001", "GHSA-RESOLVED"))
        evaluation = evaluate_dependency_health_regression(
            baseline=_snapshot(
                (
                    _finding("GHSA-TARGET", aliases=("CVE-2026-0001",)),
                    _finding("GHSA-RESOLVED"),
                )
            ),
            candidate=_candidate(
                (_finding("GHSA-MOVED", aliases=("CVE-2026-0001",), package="moved"),)
            ),
            policy=policy,
        )

        self.assertEqual(evaluation.policy_evaluation.status, "fail")
        self.assertEqual(
            evaluation.policy_evaluation.reason_codes,
            ("introduced_high_or_critical", "target_advisory_unresolved"),
        )
        self.assertEqual(
            [target.status for target in evaluation.policy_evaluation.target_advisories],
            ["unresolved", "resolved"],
        )

    def test_target_missing_from_baseline_fails_closed(self) -> None:
        evaluation = evaluate_dependency_health_regression(
            baseline=_snapshot(()),
            candidate=_candidate(()),
            policy=DependencyHealthPolicy(target_advisory_ids=("GHSA-MISSING",)),
        )

        self.assertEqual(evaluation.policy_evaluation.status, "fail")
        self.assertEqual(
            evaluation.policy_evaluation.reason_codes,
            ("target_advisory_missing_from_baseline",),
        )

    def test_target_alias_dropout_cannot_falsely_resolve_unchanged_finding(self) -> None:
        evaluation = evaluate_dependency_health_regression(
            baseline=_snapshot((_finding("GHSA-TARGET", aliases=("CVE-2026-0001",)),)),
            candidate=_candidate((_finding("GHSA-TARGET"),)),
            policy=DependencyHealthPolicy(target_advisory_ids=("CVE-2026-0001",)),
        )

        self.assertEqual(evaluation.policy_evaluation.status, "fail")
        self.assertEqual(
            evaluation.policy_evaluation.reason_codes,
            ("target_advisory_unresolved",),
        )
        self.assertEqual(
            evaluation.policy_evaluation.target_advisories[0].status,
            "unresolved",
        )

    def test_target_advisory_version_replacement_remains_unresolved(self) -> None:
        evaluation = evaluate_dependency_health_regression(
            baseline=_snapshot((_finding("GHSA-TARGET", versions=("1.0.0",)),)),
            candidate=_candidate((_finding("GHSA-TARGET", versions=("1.0.1",)),)),
            policy=DependencyHealthPolicy(target_advisory_ids=("GHSA-TARGET",)),
        )

        self.assertEqual(evaluation.policy_evaluation.status, "fail")
        self.assertEqual(
            evaluation.policy_evaluation.reason_codes,
            ("target_advisory_unresolved",),
        )
        self.assertEqual(
            evaluation.policy_evaluation.target_advisories[0].status,
            "unresolved",
        )

    def test_target_alias_dropout_cannot_falsely_resolve_moved_finding(self) -> None:
        evaluation = evaluate_dependency_health_regression(
            baseline=_snapshot(
                (
                    _finding(
                        "GHSA-TARGET",
                        aliases=("CVE-2026-0001",),
                        package="old-package",
                        severity="moderate",
                    ),
                )
            ),
            candidate=_candidate(
                (
                    _finding(
                        "GHSA-TARGET",
                        package="new-package",
                        manifest_path="other-lock.json",
                        severity="moderate",
                    ),
                )
            ),
            policy=DependencyHealthPolicy(target_advisory_ids=("CVE-2026-0001",)),
        )

        self.assertEqual(evaluation.policy_evaluation.status, "fail")
        self.assertEqual(
            evaluation.policy_evaluation.reason_codes,
            ("target_advisory_unresolved",),
        )
        self.assertEqual(
            evaluation.policy_evaluation.target_advisories[0].status,
            "unresolved",
        )

    def test_target_alias_closure_is_transitive_across_baseline_findings(self) -> None:
        evaluation = evaluate_dependency_health_regression(
            baseline=_snapshot(
                (
                    _finding(
                        "GHSA-A",
                        aliases=("CVE-2026-0001",),
                        package="removed-package",
                        severity="moderate",
                    ),
                    _finding(
                        "OSV-B",
                        aliases=("GHSA-A",),
                        package="persisting-package",
                        severity="moderate",
                    ),
                )
            ),
            candidate=_candidate(
                (
                    _finding(
                        "OSV-B",
                        package="persisting-package",
                        severity="moderate",
                    ),
                )
            ),
            policy=DependencyHealthPolicy(target_advisory_ids=("CVE-2026-0001",)),
        )

        self.assertEqual(evaluation.policy_evaluation.status, "fail")
        self.assertEqual(
            evaluation.policy_evaluation.reason_codes,
            ("target_advisory_unresolved",),
        )
        target = evaluation.policy_evaluation.target_advisories[0]
        self.assertEqual(target.status, "unresolved")
        self.assertEqual(target.baseline_matches, 2)
        self.assertEqual(target.candidate_matches, 1)

    def test_absolute_evaluation_remains_red_for_inherited_high_findings(self) -> None:
        evaluation = evaluate_dependency_health_absolute(
            _snapshot(
                (
                    _finding("GHSA-HIGH"),
                    _finding("GHSA-MODERATE", severity="moderate", package="other"),
                )
            )
        )

        self.assertEqual(evaluation.status, "fail")
        self.assertEqual(evaluation.finding_count, 2)
        self.assertEqual(
            [finding.advisory_id for finding in evaluation.blocking_findings],
            ["GHSA-HIGH"],
        )


class DependencyHealthCliTests(unittest.TestCase):
    def test_compare_cli_emits_json_and_passes_for_unchanged_inherited_finding(self) -> None:
        baseline = _snapshot((_finding("GHSA-EXISTING"),))
        candidate = _candidate((_finding("GHSA-EXISTING", versions=("1.0.1",)),))

        result = self._invoke_compare(baseline, candidate)

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["policy_evaluation"]["status"], "pass")

    def test_compare_cli_emits_json_before_policy_failure_exit(self) -> None:
        result = self._invoke_compare(
            _snapshot(()),
            _candidate((_finding("GHSA-INTRODUCED"),)),
        )

        self.assertEqual(result.exit_code, 1)
        payload = json.loads(result.output)
        self.assertEqual(payload["policy_evaluation"]["status"], "fail")

    def test_compare_cli_rejects_incompatible_provenance_without_partial_json(self) -> None:
        result = self._invoke_compare(
            _snapshot(()),
            _candidate((), advisory_revision="revision-2"),
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertTrue(result.output.startswith("Error:"), result.output)

    def test_assess_cli_emits_json_before_absolute_failure_exit(self) -> None:
        snapshot = _snapshot((_finding("GHSA-EXISTING"),))
        with TemporaryDirectory() as temp_dir:
            snapshot_file = Path(temp_dir) / "snapshot.json"
            snapshot_file.write_text(
                json.dumps(snapshot.model_dump(mode="json")),
                encoding="utf-8",
            )
            result = CliRunner().invoke(
                main,
                ["dependency-health", "assess", "--snapshot", str(snapshot_file)],
            )

        self.assertEqual(result.exit_code, 1)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "fail")

    def _invoke_compare(
        self,
        baseline: DependencyHealthSnapshot,
        candidate: DependencyHealthSnapshot,
    ) -> Result:
        with TemporaryDirectory() as temp_dir:
            baseline_file = Path(temp_dir) / "baseline.json"
            candidate_file = Path(temp_dir) / "candidate.json"
            baseline_file.write_text(
                json.dumps(baseline.model_dump(mode="json")),
                encoding="utf-8",
            )
            candidate_file.write_text(
                json.dumps(candidate.model_dump(mode="json")),
                encoding="utf-8",
            )
            return CliRunner().invoke(
                main,
                [
                    "dependency-health",
                    "compare",
                    "--baseline-snapshot",
                    str(baseline_file),
                    "--candidate-snapshot",
                    str(candidate_file),
                ],
            )


if __name__ == "__main__":
    unittest.main()
