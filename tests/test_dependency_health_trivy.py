from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.dependency_health import (
    DependencyHealthSnapshot,
    DependencyHealthSnapshotProvenance,
    extract_dependency_health_advisory_ids,
)
from control_plane.contracts.dependency_health_trivy import (
    dependency_health_snapshot_from_trivy_report,
)


BASE_COMMIT = "a" * 40
CANDIDATE_COMMIT = "b" * 40
SCAN_CONFIGURATION_SHA256 = "c" * 64


def _provenance(
    *,
    source_commit: str = BASE_COMMIT,
    baseline_commit: str = "",
) -> DependencyHealthSnapshotProvenance:
    return DependencyHealthSnapshotProvenance(
        repository="example/repository",
        source_commit=source_commit,
        baseline_commit=baseline_commit,
        producer="trivy",
        producer_version="0.70.0",
        advisory_source="trivy-db",
        advisory_revision="db-revision-1",
        scan_scope="production-lockfile",
        scan_configuration_sha256=SCAN_CONFIGURATION_SHA256,
    )


def _vulnerability(
    advisory_id: str,
    *,
    package: str = "next",
    version: str = "16.2.10",
    severity: str = "HIGH",
    references: list[str] | None = None,
) -> dict[str, object]:
    return {
        "VulnerabilityID": advisory_id,
        "PkgName": package,
        "InstalledVersion": version,
        "Severity": severity,
        "PrimaryURL": f"https://avd.aquasec.com/nvd/{advisory_id.lower()}",
        "References": references or [],
    }


def _report(
    vulnerabilities: list[dict[str, object]],
    *,
    target: str = "package-lock.json",
    ecosystem: str = "node-pkg",
) -> dict[str, object]:
    packages = sorted(
        {
            (
                str(vulnerability["PkgName"]),
                str(vulnerability["InstalledVersion"]),
            )
            for vulnerability in vulnerabilities
        }
        or {("next", "16.2.10")}
    )
    return {
        "SchemaVersion": 2,
        "CreatedAt": "2026-07-31T13:00:00Z",
        "ArtifactName": ".",
        "ArtifactType": "filesystem",
        "Results": [
            {
                "Target": target,
                "Class": "lang-pkgs",
                "Type": ecosystem,
                "Packages": [
                    {
                        "Name": package_name,
                        "Version": package_version,
                    }
                    for package_name, package_version in packages
                ],
                "Vulnerabilities": vulnerabilities,
            }
        ],
    }


class DependencyHealthTrivyAdapterTests(unittest.TestCase):
    def test_adapter_normalizes_aliases_and_aggregates_occurrences(self) -> None:
        report = _report(
            [
                _vulnerability(
                    "CVE-2026-12345",
                    severity="MEDIUM",
                    references=[
                        "https://github.com/advisories/GHSA-aaaa-bbbb-cccc",
                    ],
                ),
                _vulnerability(
                    "CVE-2026-12345",
                    version="16.2.11",
                    references=[
                        "https://github.com/advisories/GHSA-AAAA-BBBB-CCCC",
                    ],
                ),
            ]
        )

        snapshot = dependency_health_snapshot_from_trivy_report(
            report=report,
            provenance=_provenance(),
        )

        self.assertEqual(snapshot.generated_at, "2026-07-31T13:00:00Z")
        self.assertEqual(len(snapshot.findings), 1)
        finding = snapshot.findings[0]
        self.assertEqual(finding.advisory_id, "CVE-2026-12345")
        self.assertEqual(finding.aliases, ("GHSA-AAAA-BBBB-CCCC",))
        self.assertEqual(finding.ecosystem, "npm")
        self.assertEqual(finding.versions, ("16.2.10", "16.2.11"))
        self.assertEqual(finding.occurrence_count, 2)
        self.assertEqual(finding.severity, "high")

    def test_adapter_uses_stable_safe_path_for_unsafe_target(self) -> None:
        snapshot = dependency_health_snapshot_from_trivy_report(
            report=_report(
                [_vulnerability("GHSA-AAAA-BBBB-CCCC")],
                target="Node.js (node-pkg)",
            ),
            provenance=_provenance(),
        )

        self.assertRegex(
            snapshot.findings[0].manifest_path,
            r"^trivy-targets/Node\.js-node-pkg-[0-9a-f]{16}\.json$",
        )

    def test_adapter_does_not_treat_absolute_target_as_repo_relative(self) -> None:
        snapshot = dependency_health_snapshot_from_trivy_report(
            report=_report(
                [_vulnerability("GHSA-AAAA-BBBB-CCCC")],
                target="/home/runner/work/product/package-lock.json",
            ),
            provenance=_provenance(),
        )

        manifest_path = snapshot.findings[0].manifest_path
        self.assertRegex(
            manifest_path,
            r"^trivy-targets/home-runner-work-product-package-lock\.json-[0-9a-f]{16}\.json$",
        )
        self.assertNotIn("/home/runner", manifest_path)

    def test_adapter_allows_clean_result_without_vulnerabilities(self) -> None:
        snapshot = dependency_health_snapshot_from_trivy_report(
            report={
                "SchemaVersion": 2,
                "CreatedAt": "2026-07-31T13:00:00Z",
                "ArtifactName": ".",
                "ArtifactType": "filesystem",
                "Results": [
                    {
                        "Target": "package-lock.json",
                        "Class": "lang-pkgs",
                        "Type": "node-pkg",
                        "Packages": [
                            {
                                "Name": "next",
                                "Version": "16.2.11",
                            }
                        ],
                    }
                ],
            },
            provenance=_provenance(),
        )

        self.assertEqual(snapshot.findings, ())

    def test_adapter_normalizes_created_at_offset_to_utc(self) -> None:
        report = _report([])
        report["CreatedAt"] = "2026-07-31T14:28:11.136669-04:00"

        snapshot = dependency_health_snapshot_from_trivy_report(
            report=report,
            provenance=_provenance(),
        )

        self.assertEqual(snapshot.generated_at, "2026-07-31T18:28:11.136669Z")

    def test_adapter_rejects_created_at_without_offset(self) -> None:
        report = _report([])
        report["CreatedAt"] = "2026-07-31T18:28:11"

        with self.assertRaisesRegex(ValueError, "must include a UTC offset"):
            dependency_health_snapshot_from_trivy_report(
                report=report,
                provenance=_provenance(),
            )

    def test_adapter_rejects_missing_results(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires Results"):
            dependency_health_snapshot_from_trivy_report(
                report={
                    "SchemaVersion": 2,
                    "CreatedAt": "2026-07-31T13:00:00Z",
                    "ArtifactName": ".",
                    "ArtifactType": "filesystem",
                },
                provenance=_provenance(),
            )

    def test_adapter_rejects_report_without_package_inventory(self) -> None:
        report = _report([])
        result = report["Results"]
        assert isinstance(result, list)
        result_payload = result[0]
        assert isinstance(result_payload, dict)
        result_payload.pop("Packages")

        with self.assertRaisesRegex(ValueError, "--list-all-pkgs"):
            dependency_health_snapshot_from_trivy_report(
                report=report,
                provenance=_provenance(),
            )

    def test_adapter_rejects_malformed_package_inventory(self) -> None:
        report = _report([])
        result = report["Results"]
        assert isinstance(result, list)
        result_payload = result[0]
        assert isinstance(result_payload, dict)
        result_payload["Packages"] = [{}]

        with self.assertRaisesRegex(ValueError, "Trivy package name"):
            dependency_health_snapshot_from_trivy_report(
                report=report,
                provenance=_provenance(),
            )

    def test_adapter_rejects_non_vulnerability_result_classes(self) -> None:
        report = _report([])
        results = report["Results"]
        assert isinstance(results, list)
        result = results[0]
        assert isinstance(result, dict)
        result["Class"] = "config"

        with self.assertRaisesRegex(ValueError, "--scanners vulnerability"):
            dependency_health_snapshot_from_trivy_report(
                report=report,
                provenance=_provenance(),
            )

    def test_adapter_rejects_vulnerability_missing_from_package_inventory(self) -> None:
        report = _report([_vulnerability("GHSA-AAAA-BBBB-CCCC")])
        result = report["Results"]
        assert isinstance(result, list)
        result_payload = result[0]
        assert isinstance(result_payload, dict)
        result_payload["Packages"] = [{"Name": "other-package", "Version": "1.0.0"}]

        with self.assertRaisesRegex(ValueError, "absent from Packages evidence"):
            dependency_health_snapshot_from_trivy_report(
                report=report,
                provenance=_provenance(),
            )

    def test_adapter_rejects_modified_findings(self) -> None:
        report = _report([])
        result = report["Results"]
        assert isinstance(result, list)
        result_payload = result[0]
        assert isinstance(result_payload, dict)
        result_payload["ExperimentalModifiedFindings"] = [{"Status": "ignored"}]

        with self.assertRaisesRegex(ValueError, "cannot contain modified findings"):
            dependency_health_snapshot_from_trivy_report(
                report=report,
                provenance=_provenance(),
            )

    def test_adapter_rejects_empty_results(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-empty array"):
            dependency_health_snapshot_from_trivy_report(
                report={
                    "SchemaVersion": 2,
                    "CreatedAt": "2026-07-31T13:00:00Z",
                    "ArtifactName": ".",
                    "ArtifactType": "filesystem",
                    "Results": None,
                },
                provenance=_provenance(),
            )

    def test_adapter_rejects_non_integer_schema_version(self) -> None:
        report = _report([])
        report["SchemaVersion"] = "2"
        with self.assertRaisesRegex(ValueError, "integer 2"):
            dependency_health_snapshot_from_trivy_report(
                report=report,
                provenance=_provenance(),
            )

    def test_adapter_rejects_unknown_severity(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported Trivy vulnerability severity"):
            dependency_health_snapshot_from_trivy_report(
                report=_report([_vulnerability("GHSA-AAAA-BBBB-CCCC", severity="UNKNOWN")]),
                provenance=_provenance(),
            )

    def test_advisory_extraction_is_deduplicated_and_case_insensitive(self) -> None:
        self.assertEqual(
            extract_dependency_health_advisory_ids(
                "GHSA-aaaa-bbbb-cccc CVE-2026-12345 ghsa-AAAA-BBBB-CCCC"
            ),
            ("CVE-2026-12345", "GHSA-AAAA-BBBB-CCCC"),
        )


class DependencyHealthTrivyCliTests(unittest.TestCase):
    def test_trivy_snapshot_cli_emits_normalized_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_directory:
            report_file = Path(temporary_directory) / "trivy.json"
            report_file.write_text(
                json.dumps(_report([_vulnerability("GHSA-AAAA-BBBB-CCCC")])),
                encoding="utf-8",
            )
            result = CliRunner().invoke(
                main,
                [
                    "dependency-health",
                    "trivy-snapshot",
                    "--report",
                    str(report_file),
                    "--repository",
                    "example/repository",
                    "--source-commit",
                    BASE_COMMIT,
                    "--producer-version",
                    "0.70.0",
                    "--advisory-revision",
                    "db-revision-1",
                    "--scan-scope",
                    "production-lockfile",
                    "--scan-configuration-sha256",
                    SCAN_CONFIGURATION_SHA256,
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        snapshot = DependencyHealthSnapshot.model_validate(json.loads(result.output))
        self.assertEqual(snapshot.findings[0].advisory_id, "GHSA-AAAA-BBBB-CCCC")

    def test_compare_cli_extracts_target_advisory_ids_from_text(self) -> None:
        baseline = dependency_health_snapshot_from_trivy_report(
            report=_report([_vulnerability("GHSA-AAAA-BBBB-CCCC")]),
            provenance=_provenance(),
        )
        candidate = dependency_health_snapshot_from_trivy_report(
            report=_report([_vulnerability("GHSA-AAAA-BBBB-CCCC", version="16.2.11")]),
            provenance=_provenance(
                source_commit=CANDIDATE_COMMIT,
                baseline_commit=BASE_COMMIT,
            ),
        )

        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            baseline_file = temporary_root / "baseline.json"
            candidate_file = temporary_root / "candidate.json"
            advisory_text_file = temporary_root / "pull-request-body.txt"
            baseline_file.write_text(
                json.dumps(baseline.model_dump(mode="json")),
                encoding="utf-8",
            )
            candidate_file.write_text(
                json.dumps(candidate.model_dump(mode="json")),
                encoding="utf-8",
            )
            advisory_text_file.write_text(
                "Security fixes: GHSA-aaaa-bbbb-cccc",
                encoding="utf-8",
            )
            result = CliRunner().invoke(
                main,
                [
                    "dependency-health",
                    "compare",
                    "--baseline-snapshot",
                    str(baseline_file),
                    "--candidate-snapshot",
                    str(candidate_file),
                    "--target-advisory-text-file",
                    str(advisory_text_file),
                ],
            )

        self.assertEqual(result.exit_code, 1, result.output)
        evaluation = json.loads(result.output)
        self.assertEqual(
            evaluation["policy_evaluation"]["reason_codes"],
            ["target_advisory_unresolved"],
        )

    def test_compare_cli_ignores_text_advisories_absent_from_baseline(self) -> None:
        baseline = dependency_health_snapshot_from_trivy_report(
            report=_report([_vulnerability("GHSA-AAAA-BBBB-CCCC")]),
            provenance=_provenance(),
        )
        candidate = dependency_health_snapshot_from_trivy_report(
            report=_report([]),
            provenance=_provenance(
                source_commit=CANDIDATE_COMMIT,
                baseline_commit=BASE_COMMIT,
            ),
        )

        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            baseline_file = temporary_root / "baseline.json"
            candidate_file = temporary_root / "candidate.json"
            advisory_text_file = temporary_root / "pull-request-body.txt"
            baseline_file.write_text(
                json.dumps(baseline.model_dump(mode="json")),
                encoding="utf-8",
            )
            candidate_file.write_text(
                json.dumps(candidate.model_dump(mode="json")),
                encoding="utf-8",
            )
            advisory_text_file.write_text(
                "Security fixes: GHSA-aaaa-bbbb-cccc GHSA-dddd-eeee-ffff",
                encoding="utf-8",
            )
            result = CliRunner().invoke(
                main,
                [
                    "dependency-health",
                    "compare",
                    "--baseline-snapshot",
                    str(baseline_file),
                    "--candidate-snapshot",
                    str(candidate_file),
                    "--target-advisory-text-file",
                    str(advisory_text_file),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        evaluation = json.loads(result.output)
        self.assertEqual(
            evaluation["policy"]["target_advisory_ids"],
            ["GHSA-AAAA-BBBB-CCCC"],
        )

    def test_compare_cli_rejects_policy_file_with_target_options(self) -> None:
        snapshot = dependency_health_snapshot_from_trivy_report(
            report=_report([]),
            provenance=_provenance(),
        )
        candidate = snapshot.model_copy(
            update={
                "provenance": _provenance(
                    source_commit=CANDIDATE_COMMIT,
                    baseline_commit=BASE_COMMIT,
                )
            }
        )

        with TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            baseline_file = temporary_root / "baseline.json"
            candidate_file = temporary_root / "candidate.json"
            policy_file = temporary_root / "policy.json"
            baseline_file.write_text(
                json.dumps(snapshot.model_dump(mode="json")),
                encoding="utf-8",
            )
            candidate_file.write_text(
                json.dumps(candidate.model_dump(mode="json")),
                encoding="utf-8",
            )
            policy_file.write_text(
                json.dumps({"target_advisory_ids": []}),
                encoding="utf-8",
            )
            result = CliRunner().invoke(
                main,
                [
                    "dependency-health",
                    "compare",
                    "--baseline-snapshot",
                    str(baseline_file),
                    "--candidate-snapshot",
                    str(candidate_file),
                    "--policy-file",
                    str(policy_file),
                    "--target-advisory-id",
                    "GHSA-AAAA-BBBB-CCCC",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("cannot be combined", result.output)


if __name__ == "__main__":
    unittest.main()
