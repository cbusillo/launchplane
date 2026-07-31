from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import re

from control_plane.contracts.artifact_dependency_provenance import (
    normalize_artifact_relative_path,
)
from control_plane.contracts.dependency_health import (
    DependencyHealthFinding,
    DependencyHealthFindingIdentity,
    DependencyHealthSeverity,
    DependencyHealthSnapshot,
    DependencyHealthSnapshotProvenance,
    extract_dependency_health_advisory_ids,
)


_TRIVY_SEVERITY_MAP: dict[str, DependencyHealthSeverity] = {
    "LOW": "low",
    "MEDIUM": "moderate",
    "HIGH": "high",
    "CRITICAL": "critical",
}
_TRIVY_SEVERITY_RANK: dict[DependencyHealthSeverity, int] = {
    "low": 1,
    "moderate": 2,
    "high": 3,
    "critical": 4,
}
_TRIVY_ECOSYSTEM_MAP = {
    "node-pkg": "npm",
    "npm": "npm",
    "pip": "pypi",
    "pipenv": "pypi",
    "poetry": "pypi",
    "python-pkg": "pypi",
}
_TRIVY_PACKAGE_RESULT_CLASSES = frozenset({"lang-pkgs", "os-pkgs"})
_UNSAFE_TARGET_CHARACTER_PATTERN = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class _TrivyFindingAccumulator:
    advisory_id: str
    ecosystem: str
    package: str
    manifest_path: str
    severity: DependencyHealthSeverity
    aliases: set[str] = field(default_factory=set)
    versions: set[str] = field(default_factory=set)
    occurrence_count: int = 0

    def add(self, finding: DependencyHealthFinding) -> None:
        self.aliases.update(finding.aliases)
        self.versions.update(finding.versions)
        self.occurrence_count += finding.occurrence_count
        if _TRIVY_SEVERITY_RANK[finding.severity] > _TRIVY_SEVERITY_RANK[self.severity]:
            self.severity = finding.severity

    def build(self) -> DependencyHealthFinding:
        return DependencyHealthFinding(
            advisory_id=self.advisory_id,
            aliases=tuple(sorted(self.aliases - {self.advisory_id})),
            ecosystem=self.ecosystem,
            package=self.package,
            versions=tuple(sorted(self.versions)),
            occurrence_count=self.occurrence_count,
            manifest_path=self.manifest_path,
            severity=self.severity,
        )


def dependency_health_snapshot_from_trivy_report(
    *,
    report: Mapping[str, object],
    provenance: DependencyHealthSnapshotProvenance,
) -> DependencyHealthSnapshot:
    schema_version = report.get("SchemaVersion")
    if type(schema_version) is not int or schema_version != 2:
        raise ValueError("Trivy report SchemaVersion must be the integer 2")
    generated_at = _required_string(report, "CreatedAt", label="Trivy report CreatedAt")
    _required_string(report, "ArtifactName", label="Trivy report ArtifactName")
    _required_string(report, "ArtifactType", label="Trivy report ArtifactType")
    if "Results" not in report:
        raise ValueError("Trivy report requires Results")
    raw_results = report["Results"]
    if isinstance(raw_results, list) and raw_results:
        results = raw_results
    else:
        raise ValueError("Trivy report Results must be a non-empty array")

    accumulators: dict[DependencyHealthFindingIdentity, _TrivyFindingAccumulator] = {}
    for result_index, raw_result in enumerate(results):
        result = _required_mapping(raw_result, label=f"Trivy result {result_index}")
        result_class = (
            _required_string(
                result,
                "Class",
                label=f"Trivy result {result_index} Class",
            )
            .strip()
            .lower()
        )
        if result_class not in _TRIVY_PACKAGE_RESULT_CLASSES:
            raise ValueError(
                "Trivy dependency health requires vulnerability package results only; "
                "run Trivy with --scanners vulnerability"
            )
        ecosystem = _normalize_trivy_ecosystem(
            _required_string(result, "Type", label=f"Trivy result {result_index} Type")
        )
        manifest_path = _normalize_trivy_target(
            _required_string(result, "Target", label=f"Trivy result {result_index} Target")
        )
        raw_packages = result.get("Packages")
        if not isinstance(raw_packages, list) or not raw_packages:
            raise ValueError(
                f"Trivy result {result_index} requires non-empty Packages evidence; "
                "run Trivy with --list-all-pkgs"
            )
        package_inventory = _trivy_package_inventory(
            raw_packages,
            result_index=result_index,
        )
        modified_findings = result.get("ExperimentalModifiedFindings")
        if modified_findings is not None:
            if not isinstance(modified_findings, list):
                raise ValueError(
                    f"Trivy result {result_index} ExperimentalModifiedFindings must be an array"
                )
            if modified_findings:
                raise ValueError("Trivy dependency health reports cannot contain modified findings")
        raw_vulnerabilities = result.get("Vulnerabilities")
        if raw_vulnerabilities is None:
            continue
        if not isinstance(raw_vulnerabilities, list):
            raise ValueError(
                f"Trivy result {result_index} Vulnerabilities must be an array or null"
            )
        if not raw_vulnerabilities:
            continue
        for vulnerability_index, raw_vulnerability in enumerate(raw_vulnerabilities):
            vulnerability = _required_mapping(
                raw_vulnerability,
                label=f"Trivy result {result_index} vulnerability {vulnerability_index}",
            )
            advisory_id = _required_string(
                vulnerability,
                "VulnerabilityID",
                label="Trivy vulnerability ID",
            )
            package_name = _required_string(
                vulnerability,
                "PkgName",
                label="Trivy vulnerability package",
            )
            installed_version = _required_string(
                vulnerability,
                "InstalledVersion",
                label="Trivy vulnerability installed version",
            )
            if (package_name, installed_version) not in package_inventory:
                raise ValueError(
                    "Trivy vulnerability package/version is absent from Packages evidence"
                )
            finding = DependencyHealthFinding(
                advisory_id=advisory_id,
                aliases=_trivy_advisory_aliases(vulnerability, advisory_id=advisory_id),
                ecosystem=ecosystem,
                package=package_name,
                versions=(installed_version,),
                occurrence_count=1,
                manifest_path=manifest_path,
                severity=_normalize_trivy_severity(
                    _required_string(
                        vulnerability,
                        "Severity",
                        label="Trivy vulnerability severity",
                    )
                ),
            )
            identity = finding.identity()
            accumulator = accumulators.get(identity)
            if accumulator is None:
                accumulator = _TrivyFindingAccumulator(
                    advisory_id=finding.advisory_id,
                    ecosystem=finding.ecosystem,
                    package=finding.package,
                    manifest_path=finding.manifest_path,
                    severity=finding.severity,
                )
                accumulators[identity] = accumulator
            accumulator.add(finding)

    return DependencyHealthSnapshot(
        generated_at=generated_at,
        provenance=provenance,
        findings=tuple(accumulator.build() for accumulator in accumulators.values()),
    )


def _trivy_advisory_aliases(
    vulnerability: Mapping[str, object],
    *,
    advisory_id: str,
) -> tuple[str, ...]:
    reference_text: list[str] = []
    primary_url = vulnerability.get("PrimaryURL")
    if isinstance(primary_url, str):
        reference_text.append(primary_url)
    references = vulnerability.get("References")
    if references is not None:
        if not isinstance(references, list) or not all(
            isinstance(reference, str) for reference in references
        ):
            raise ValueError("Trivy vulnerability References must be an array of strings")
        reference_text.extend(references)
    aliases = set(extract_dependency_health_advisory_ids("\n".join(reference_text)))
    aliases.discard(advisory_id.strip().upper())
    return tuple(sorted(aliases))


def _trivy_package_inventory(
    raw_packages: list[object],
    *,
    result_index: int,
) -> frozenset[tuple[str, str]]:
    packages: set[tuple[str, str]] = set()
    for package_index, raw_package in enumerate(raw_packages):
        package = _required_mapping(
            raw_package,
            label=f"Trivy result {result_index} package {package_index}",
        )
        packages.add(
            (
                _required_string(package, "Name", label="Trivy package name"),
                _required_string(package, "Version", label="Trivy package version"),
            )
        )
    return frozenset(packages)


def _normalize_trivy_ecosystem(value: str) -> str:
    normalized = value.strip().lower()
    mapped = _TRIVY_ECOSYSTEM_MAP.get(normalized, normalized)
    if not mapped:
        raise ValueError("Trivy result Type must be non-empty")
    return mapped


def _normalize_trivy_severity(value: str) -> DependencyHealthSeverity:
    normalized = value.strip().upper()
    try:
        return _TRIVY_SEVERITY_MAP[normalized]
    except KeyError as error:
        raise ValueError(f"unsupported Trivy vulnerability severity: {normalized}") from error


def _normalize_trivy_target(value: str) -> str:
    normalized = value.strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    try:
        return normalize_artifact_relative_path(
            normalized,
            label="Trivy result target",
        )
    except ValueError:
        target_digest = hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:16]
        target_slug = _UNSAFE_TARGET_CHARACTER_PATTERN.sub("-", value.strip()).strip(".-")
        if not target_slug:
            target_slug = "target"
        return f"trivy-targets/{target_slug[:80]}-{target_digest}.json"


def _required_mapping(value: object, *, label: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _required_string(
    payload: Mapping[str, object],
    key: str,
    *,
    label: str,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value
