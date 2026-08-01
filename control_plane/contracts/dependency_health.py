from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from control_plane.contracts.artifact_dependency_provenance import (
    normalize_artifact_git_commit,
    normalize_artifact_relative_path,
    normalize_artifact_repository_identity,
    normalize_artifact_sha256,
)


def _require_integer_schema_version(value: object) -> object:
    if type(value) is not int:
        raise ValueError("dependency health schema_version must be an integer")
    return value


DependencyHealthSchemaVersion = Annotated[
    Literal[1],
    BeforeValidator(_require_integer_schema_version),
]
DependencyHealthSeverity = Literal["low", "moderate", "high", "critical"]
DependencyHealthStatus = Literal["pass", "fail"]
DependencyHealthPolicyMode = Literal["no_high_or_critical_regressions"]
DependencyHealthTargetStatus = Literal[
    "resolved",
    "unresolved",
    "missing_from_baseline",
]
DependencyHealthReasonCode = Literal[
    "introduced_high_or_critical",
    "target_advisory_missing_from_baseline",
    "target_advisory_unresolved",
    "worsened_to_high_or_critical",
]
DependencyHealthFindingIdentity = tuple[str, str, str, str]

_ADVISORY_ID_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9._:+-]*$")
_ADVISORY_REFERENCE_PATTERN = re.compile(
    r"(?i)\b(?:GHSA-[0-9A-Z]{4}(?:-[0-9A-Z]{4}){2}|CVE-[0-9]{4}-[0-9]{4,}|"
    r"PYSEC-[0-9]{4}-[0-9]+|OSV-[0-9A-Z._:+-]+)\b"
)
_ECOSYSTEM_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._+-]*$")
_VISIBLE_TOKEN_PATTERN = re.compile(r"^[\x21-\x7e]+$")
_BLOCKING_SEVERITIES: frozenset[DependencyHealthSeverity] = frozenset({"high", "critical"})
_SEVERITY_RANK: dict[DependencyHealthSeverity, int] = {
    "low": 1,
    "moderate": 2,
    "high": 3,
    "critical": 4,
}
_NON_COMPARISON_PROVENANCE_FIELDS = frozenset({"source_commit", "baseline_commit"})


class DependencyHealthProvenanceMismatch(ValueError):
    def __init__(self, mismatched_fields: tuple[str, ...]) -> None:
        self.mismatched_fields = tuple(sorted(set(mismatched_fields)))
        joined_fields = ", ".join(self.mismatched_fields)
        super().__init__(
            f"dependency health snapshots have incompatible provenance fields: {joined_fields}"
        )


def extract_dependency_health_advisory_ids(value: str) -> tuple[str, ...]:
    return tuple(
        sorted({match.group(0).upper() for match in _ADVISORY_REFERENCE_PATTERN.finditer(value)})
    )


class DependencyHealthFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    advisory_id: str
    aliases: tuple[str, ...] = ()
    ecosystem: str
    package: str
    versions: tuple[str, ...]
    occurrence_count: int = Field(default=1, ge=1, strict=True)
    manifest_path: str
    severity: DependencyHealthSeverity

    @model_validator(mode="after")
    def _normalize_finding(self) -> Self:
        self.advisory_id = _normalize_advisory_id(self.advisory_id)
        aliases = tuple(sorted({_normalize_advisory_id(alias) for alias in self.aliases}))
        if self.advisory_id in aliases:
            raise ValueError("dependency health aliases cannot repeat advisory_id")
        self.aliases = aliases

        self.ecosystem = self.ecosystem.strip().lower()
        if not _ECOSYSTEM_PATTERN.fullmatch(self.ecosystem):
            raise ValueError("dependency health ecosystem must use canonical lowercase syntax")
        self.package = _normalize_visible_token(
            self.package,
            label="dependency health package",
        )
        versions = tuple(
            sorted(
                {
                    _normalize_visible_token(
                        version,
                        label="dependency health package version",
                    )
                    for version in self.versions
                }
            )
        )
        if not versions:
            raise ValueError("dependency health finding requires at least one package version")
        self.versions = versions
        self.manifest_path = normalize_artifact_relative_path(
            self.manifest_path,
            label="dependency health manifest path",
        )
        return self

    def identity(self) -> DependencyHealthFindingIdentity:
        return (
            self.advisory_id,
            self.ecosystem,
            self.package,
            self.manifest_path,
        )

    def matches_advisory(self, advisory_id: str) -> bool:
        normalized = _normalize_advisory_id(advisory_id)
        return normalized == self.advisory_id or normalized in self.aliases

    def advisory_ids(self) -> frozenset[str]:
        return frozenset((self.advisory_id, *self.aliases))


class DependencyHealthSnapshotProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    source_commit: str
    baseline_commit: str = ""
    producer: str
    producer_version: str
    advisory_source: str
    advisory_revision: str
    scan_scope: str
    scan_configuration_sha256: str

    @model_validator(mode="after")
    def _normalize_provenance(self) -> Self:
        self.repository = normalize_artifact_repository_identity(
            self.repository,
            label="dependency health repository",
        )
        self.source_commit = normalize_artifact_git_commit(
            self.source_commit,
            label="dependency health source_commit",
        )
        self.baseline_commit = self.baseline_commit.strip()
        if self.baseline_commit:
            self.baseline_commit = normalize_artifact_git_commit(
                self.baseline_commit,
                label="dependency health baseline_commit",
            )
        self.producer = _normalize_visible_token(
            self.producer,
            label="dependency health producer",
        )
        self.producer_version = _normalize_visible_token(
            self.producer_version,
            label="dependency health producer version",
        )
        self.advisory_source = _normalize_visible_token(
            self.advisory_source,
            label="dependency health advisory source",
        )
        self.advisory_revision = _normalize_visible_token(
            self.advisory_revision,
            label="dependency health advisory revision",
        )
        self.scan_scope = _normalize_visible_token(
            self.scan_scope,
            label="dependency health scan scope",
        )
        self.scan_configuration_sha256 = normalize_artifact_sha256(
            self.scan_configuration_sha256,
            label="dependency health scan configuration sha256",
        )
        return self


class DependencyHealthSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: DependencyHealthSchemaVersion = 1
    generated_at: str
    provenance: DependencyHealthSnapshotProvenance
    findings: tuple[DependencyHealthFinding, ...] = ()

    @model_validator(mode="after")
    def _normalize_snapshot(self) -> Self:
        self.generated_at = _normalize_utc_timestamp(self.generated_at)
        findings = tuple(sorted(self.findings, key=lambda finding: finding.identity()))
        identities = [finding.identity() for finding in findings]
        if len(identities) != len(set(identities)):
            raise ValueError("dependency health snapshot cannot contain duplicate findings")
        self.findings = findings
        return self


class DependencyHealthPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: DependencyHealthSchemaVersion = 1
    mode: DependencyHealthPolicyMode = "no_high_or_critical_regressions"
    target_advisory_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_policy(self) -> Self:
        self.target_advisory_ids = tuple(
            sorted({_normalize_advisory_id(value) for value in self.target_advisory_ids})
        )
        return self


class DependencyHealthMatchedFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline: DependencyHealthFinding
    candidate: DependencyHealthFinding

    @model_validator(mode="after")
    def _validate_identity(self) -> Self:
        if self.baseline.identity() != self.candidate.identity():
            raise ValueError("matched dependency health findings must share one identity")
        return self


class DependencyHealthComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: DependencyHealthSchemaVersion = 1
    baseline_provenance: DependencyHealthSnapshotProvenance
    candidate_provenance: DependencyHealthSnapshotProvenance
    introduced: tuple[DependencyHealthFinding, ...] = ()
    worsened: tuple[DependencyHealthMatchedFinding, ...] = ()
    resolved: tuple[DependencyHealthFinding, ...] = ()
    unchanged: tuple[DependencyHealthMatchedFinding, ...] = ()


class DependencyHealthTargetAdvisoryEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    advisory_id: str
    baseline_matches: int = Field(ge=0, strict=True)
    candidate_matches: int = Field(ge=0, strict=True)
    status: DependencyHealthTargetStatus


class DependencyHealthPolicyEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: DependencyHealthStatus
    reason_codes: tuple[DependencyHealthReasonCode, ...] = ()
    target_advisories: tuple[DependencyHealthTargetAdvisoryEvaluation, ...] = ()
    summary: str


class DependencyHealthEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: DependencyHealthSchemaVersion = 1
    comparison: DependencyHealthComparison
    policy: DependencyHealthPolicy
    policy_evaluation: DependencyHealthPolicyEvaluation


class DependencyHealthAbsoluteEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: DependencyHealthSchemaVersion = 1
    snapshot_provenance: DependencyHealthSnapshotProvenance
    generated_at: str
    status: DependencyHealthStatus
    finding_count: int = Field(ge=0, strict=True)
    blocking_findings: tuple[DependencyHealthFinding, ...] = ()
    summary: str


def compare_dependency_health_snapshots(
    *,
    baseline: DependencyHealthSnapshot,
    candidate: DependencyHealthSnapshot,
) -> DependencyHealthComparison:
    _require_compatible_provenance(baseline=baseline, candidate=candidate)
    baseline_findings = {finding.identity(): finding for finding in baseline.findings}
    candidate_findings = {finding.identity(): finding for finding in candidate.findings}

    introduced: list[DependencyHealthFinding] = []
    worsened: list[DependencyHealthMatchedFinding] = []
    resolved: list[DependencyHealthFinding] = []
    unchanged: list[DependencyHealthMatchedFinding] = []
    for identity in sorted(set(baseline_findings) | set(candidate_findings)):
        baseline_finding = baseline_findings.get(identity)
        candidate_finding = candidate_findings.get(identity)
        if baseline_finding is None:
            if candidate_finding is None:
                raise AssertionError("dependency health comparison identity has no finding")
            introduced.append(candidate_finding)
            continue
        if candidate_finding is None:
            resolved.append(baseline_finding)
            continue
        matched = DependencyHealthMatchedFinding(
            baseline=baseline_finding,
            candidate=candidate_finding,
        )
        if _finding_worsened(
            baseline=baseline_finding,
            candidate=candidate_finding,
        ):
            worsened.append(matched)
        else:
            unchanged.append(matched)

    return DependencyHealthComparison(
        baseline_provenance=baseline.provenance,
        candidate_provenance=candidate.provenance,
        introduced=tuple(introduced),
        worsened=tuple(worsened),
        resolved=tuple(resolved),
        unchanged=tuple(unchanged),
    )


def evaluate_dependency_health_regression(
    *,
    baseline: DependencyHealthSnapshot,
    candidate: DependencyHealthSnapshot,
    policy: DependencyHealthPolicy | None = None,
) -> DependencyHealthEvaluation:
    effective_policy = policy or DependencyHealthPolicy()
    comparison = compare_dependency_health_snapshots(
        baseline=baseline,
        candidate=candidate,
    )
    reason_codes: set[DependencyHealthReasonCode] = set()
    if any(finding.severity in _BLOCKING_SEVERITIES for finding in comparison.introduced):
        reason_codes.add("introduced_high_or_critical")
    if any(finding.candidate.severity in _BLOCKING_SEVERITIES for finding in comparison.worsened):
        reason_codes.add("worsened_to_high_or_critical")

    target_evaluations: list[DependencyHealthTargetAdvisoryEvaluation] = []
    for advisory_id in effective_policy.target_advisory_ids:
        equivalent_advisory_ids = _advisory_equivalence_component(
            findings=baseline.findings,
            advisory_id=advisory_id,
        )
        baseline_matches = tuple(
            finding
            for finding in baseline.findings
            if finding.advisory_ids() & equivalent_advisory_ids
        )
        candidate_matches = sum(
            bool(finding.advisory_ids() & equivalent_advisory_ids) for finding in candidate.findings
        )
        if not baseline_matches:
            target_status: DependencyHealthTargetStatus = "missing_from_baseline"
            reason_codes.add("target_advisory_missing_from_baseline")
        elif candidate_matches:
            target_status = "unresolved"
            reason_codes.add("target_advisory_unresolved")
        else:
            target_status = "resolved"
        target_evaluations.append(
            DependencyHealthTargetAdvisoryEvaluation(
                advisory_id=advisory_id,
                baseline_matches=len(baseline_matches),
                candidate_matches=candidate_matches,
                status=target_status,
            )
        )

    status: DependencyHealthStatus = "fail" if reason_codes else "pass"
    summary = (
        "dependency health regression policy failed"
        if reason_codes
        else "dependency health regression policy passed"
    )
    return DependencyHealthEvaluation(
        comparison=comparison,
        policy=effective_policy,
        policy_evaluation=DependencyHealthPolicyEvaluation(
            status=status,
            reason_codes=tuple(sorted(reason_codes)),
            target_advisories=tuple(target_evaluations),
            summary=summary,
        ),
    )


def evaluate_dependency_health_absolute(
    snapshot: DependencyHealthSnapshot,
) -> DependencyHealthAbsoluteEvaluation:
    blocking_findings = tuple(
        finding for finding in snapshot.findings if finding.severity in _BLOCKING_SEVERITIES
    )
    status: DependencyHealthStatus = "fail" if blocking_findings else "pass"
    summary = (
        "dependency health absolute policy failed"
        if blocking_findings
        else "dependency health absolute policy passed"
    )
    return DependencyHealthAbsoluteEvaluation(
        snapshot_provenance=snapshot.provenance,
        generated_at=snapshot.generated_at,
        status=status,
        finding_count=len(snapshot.findings),
        blocking_findings=blocking_findings,
        summary=summary,
    )


def _advisory_equivalence_component(
    *,
    findings: tuple[DependencyHealthFinding, ...],
    advisory_id: str,
) -> frozenset[str]:
    equivalent_advisory_ids = {_normalize_advisory_id(advisory_id)}
    while True:
        expanded_advisory_ids = set(equivalent_advisory_ids)
        for finding in findings:
            finding_advisory_ids = finding.advisory_ids()
            if finding_advisory_ids & equivalent_advisory_ids:
                expanded_advisory_ids.update(finding_advisory_ids)
        if expanded_advisory_ids == equivalent_advisory_ids:
            return frozenset(equivalent_advisory_ids)
        equivalent_advisory_ids = expanded_advisory_ids


def _require_compatible_provenance(
    *,
    baseline: DependencyHealthSnapshot,
    candidate: DependencyHealthSnapshot,
) -> None:
    excluded_fields = set(_NON_COMPARISON_PROVENANCE_FIELDS)
    baseline_payload = baseline.provenance.model_dump(exclude=excluded_fields)
    candidate_payload = candidate.provenance.model_dump(exclude=excluded_fields)
    mismatched_fields = [
        field_name
        for field_name in sorted(set(baseline_payload) | set(candidate_payload))
        if baseline_payload.get(field_name) != candidate_payload.get(field_name)
    ]
    if candidate.provenance.baseline_commit != baseline.provenance.source_commit:
        mismatched_fields.append("candidate_baseline_commit")
    if mismatched_fields:
        raise DependencyHealthProvenanceMismatch(tuple(mismatched_fields))


def _finding_worsened(
    *,
    baseline: DependencyHealthFinding,
    candidate: DependencyHealthFinding,
) -> bool:
    return any(
        (
            _SEVERITY_RANK[candidate.severity] > _SEVERITY_RANK[baseline.severity],
            len(candidate.versions) > len(baseline.versions),
            candidate.occurrence_count > baseline.occurrence_count,
        )
    )


def _normalize_advisory_id(value: str) -> str:
    normalized = value.strip().upper()
    if not _ADVISORY_ID_PATTERN.fullmatch(normalized):
        raise ValueError("dependency health advisory id contains invalid characters")
    return normalized


def _normalize_visible_token(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not _VISIBLE_TOKEN_PATTERN.fullmatch(normalized):
        raise ValueError(f"{label} must be a non-empty visible token without whitespace")
    return normalized


def _normalize_utc_timestamp(value: str) -> str:
    normalized = value.strip()
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("dependency health generated_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ValueError("dependency health generated_at must use UTC")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
