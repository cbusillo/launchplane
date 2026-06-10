from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import fnmatch
import json
from pathlib import Path
import re
from typing import Literal


OdooRepoFamily = Literal[
    "tenant",
    "image",
    "shared_addons",
    "devkit",
    "retired",
]


@dataclass(frozen=True)
class OdooOwnershipRepoPolicy:
    repository: str
    family: OdooRepoFamily
    allowed_path_globs: tuple[str, ...] = ()


@dataclass(frozen=True)
class OdooOwnershipFinding:
    repository: str
    family: OdooRepoFamily
    path: str
    line: int
    rule_id: str
    severity: Literal["error", "warning"]
    message: str
    evidence: str


@dataclass(frozen=True)
class OdooOwnershipScanResult:
    status: Literal["pass", "fail"]
    repository_count: int
    scanned_file_count: int
    findings: tuple[OdooOwnershipFinding, ...]

    def to_json_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "repository_count": self.repository_count,
            "scanned_file_count": self.scanned_file_count,
            "findings": [
                {
                    "repository": finding.repository,
                    "family": finding.family,
                    "path": finding.path,
                    "line": finding.line,
                    "rule_id": finding.rule_id,
                    "severity": finding.severity,
                    "message": finding.message,
                    "evidence": finding.evidence,
                }
                for finding in self.findings
            ],
        }


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    message: str
    pattern: re.Pattern[str]
    families: tuple[OdooRepoFamily, ...]
    path_globs: tuple[str, ...] = ("**/*",)
    allowed_path_globs: tuple[str, ...] = ()
    allowed_line_patterns: tuple[re.Pattern[str], ...] = ()
    severity: Literal["error", "warning"] = "error"


DEFAULT_ODOO_REPO_POLICIES: tuple[OdooOwnershipRepoPolicy, ...] = (
    OdooOwnershipRepoPolicy("odoo-tenant-cm", "tenant"),
    OdooOwnershipRepoPolicy("odoo-tenant-cm-website", "tenant"),
    OdooOwnershipRepoPolicy("odoo-tenant-opw", "tenant"),
    OdooOwnershipRepoPolicy("odoo-docker", "image"),
    OdooOwnershipRepoPolicy("odoo-enterprise-docker", "image"),
    OdooOwnershipRepoPolicy("odoo-shared-addons", "shared_addons"),
    OdooOwnershipRepoPolicy(
        "odoo-devkit",
        "devkit",
        allowed_path_globs=(
            "odoo_devkit/dokploy_api.py",
            "odoo_devkit/dokploy_config.py",
            "odoo_devkit/remote_runtime.py",
            "odoo_devkit/workspace_surface.py",
        ),
    ),
)


_TEXT_FILE_SUFFIXES = {
    "",
    ".cfg",
    ".ini",
    ".js",
    ".json",
    ".mjs",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}

_IGNORED_PARTS = {
    ".git",
    ".code",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}

_GLOBALLY_ALLOWED_PATH_GLOBS = (
    "addons/**",
    "**/README.md",
    "docs/**",
    "tests/**",
)

_LAUNCHPLANE_ALLOWED_LINE_PATTERNS = (
    re.compile(r"uses:\s*cbusillo/launchplane/\.github/actions/launchplane-request@"),
    re.compile(r"uses:\s*cbusillo/launchplane/\.github/workflows/reusable-odoo-"),
    re.compile(r"LAUNCHPLANE_RUNTIME_IDENTITY_JSON"),
)

_RULES: tuple[_Rule, ...] = (
    _Rule(
        rule_id="repo-local-oidc-client",
        message="Repo-local GitHub OIDC token clients are not allowed; use Launchplane's shared request action or reusable workflow.",
        pattern=re.compile(
            r"\b(?:core\.getIDToken|getIDToken\s*\(|ACTIONS_ID_TOKEN_REQUEST_URL|ACTIONS_ID_TOKEN_REQUEST_TOKEN)"
        ),
        families=("tenant", "image", "shared_addons", "retired"),
        allowed_line_patterns=_LAUNCHPLANE_ALLOWED_LINE_PATTERNS,
    ),
    _Rule(
        rule_id="repo-local-launchplane-http-client",
        message="Repo-local Launchplane HTTP clients duplicate the shared request boundary.",
        pattern=re.compile(
            r"\b(fetch|axios|requests\.(?:get|post|put|patch|delete)|urlopen|curl)\b.*\blaunchplane\b",
            re.IGNORECASE,
        ),
        families=("tenant", "image", "shared_addons", "devkit", "retired"),
        allowed_line_patterns=_LAUNCHPLANE_ALLOWED_LINE_PATTERNS,
    ),
    _Rule(
        rule_id="tenant-provider-mutation",
        message="Tenant repos must not mutate runtime providers directly; route runtime operations through Launchplane.",
        pattern=re.compile(r"\b(dokploy|DOKPLOY|docker\s+compose|ssh\s+|scp\s+|rsync\s+)\b"),
        families=("tenant",),
        allowed_path_globs=(".github/workflows/ci.yml", "**/README.md", "docs/**"),
    ),
    _Rule(
        rule_id="non-launchplane-shared-prod-mutation",
        message="Shared/prod mutation commands belong behind Launchplane service routes, not arbitrary checkout CLIs.",
        pattern=re.compile(
            r"\b(prod|production|shared)\b.*\b(apply|deploy|mutate|override|rollback|promote|restore)\b",
            re.IGNORECASE,
        ),
        families=("devkit", "retired"),
        allowed_path_globs=("tests/**", "docs/**", "**/README.md"),
    ),
    _Rule(
        rule_id="launchplane-record-derivation",
        message="Repo-local derivation of Launchplane URLs, target IDs, or durable record IDs is not allowed.",
        pattern=re.compile(
            r"\b(?:preview_url|target_id|deployment_record_id|promotion_record_id|release_tuple_id|backup_gate_record_id)\b\s*[:=]"
        ),
        families=("tenant", "image", "shared_addons", "devkit", "retired"),
        allowed_path_globs=(
            ".github/workflows/odoo-artifact-publish.yml",
            ".github/workflows/odoo-preview.yml",
            ".github/workflows/odoo-post-deploy.yml",
            ".github/workflows/odoo-prod-promotion.yml",
            ".github/workflows/odoo-prod-rollback.yml",
            "tests/**",
            "docs/**",
            "**/README.md",
        ),
        allowed_line_patterns=(
            *_LAUNCHPLANE_ALLOWED_LINE_PATTERNS,
            re.compile(r"\$\{\{\s*steps\.[^.]+\.outputs\.[a-z_]+\s*\}\}"),
            re.compile(r"result\.[a-z_]+"),
            re.compile(r"github\.event\.inputs\.promotion_record_id"),
        ),
    ),
)


def scan_odoo_ownership_boundaries(
    *,
    workspace_root: Path,
    repo_policies: Iterable[OdooOwnershipRepoPolicy] = DEFAULT_ODOO_REPO_POLICIES,
) -> OdooOwnershipScanResult:
    findings: list[OdooOwnershipFinding] = []
    scanned_file_count = 0
    resolved_workspace_root = workspace_root.expanduser().resolve()
    policies = tuple(repo_policies)
    for policy in policies:
        repo_root = resolved_workspace_root / policy.repository
        if not repo_root.exists():
            findings.append(
                OdooOwnershipFinding(
                    repository=policy.repository,
                    family=policy.family,
                    path=".",
                    line=0,
                    rule_id="repository-missing",
                    severity="error",
                    message="Configured Odoo repository was not found under the workspace root.",
                    evidence=str(repo_root),
                )
            )
            continue
        for path in _iter_scannable_files(repo_root):
            scanned_file_count += 1
            relative_path = path.relative_to(repo_root).as_posix()
            if _matches_any(
                relative_path, _GLOBALLY_ALLOWED_PATH_GLOBS + policy.allowed_path_globs
            ):
                continue
            text = _read_text(path)
            if text is None:
                continue
            findings.extend(_scan_file(policy=policy, relative_path=relative_path, text=text))
    error_findings = [finding for finding in findings if finding.severity == "error"]
    return OdooOwnershipScanResult(
        status="fail" if error_findings else "pass",
        repository_count=len(policies),
        scanned_file_count=scanned_file_count,
        findings=tuple(findings),
    )


def render_odoo_ownership_scan_text(result: OdooOwnershipScanResult) -> str:
    lines = [
        f"status: {result.status}",
        f"repositories: {result.repository_count}",
        f"scanned_files: {result.scanned_file_count}",
        f"findings: {len(result.findings)}",
    ]
    for finding in result.findings:
        location = f"{finding.repository}/{finding.path}:{finding.line}"
        lines.append(
            f"{finding.severity}: {location}: {finding.rule_id}: "
            f"{finding.message} [{finding.evidence}]"
        )
    return "\n".join(lines)


def render_odoo_ownership_scan_json(result: OdooOwnershipScanResult) -> str:
    return json.dumps(result.to_json_payload(), indent=2, sort_keys=True)


def _scan_file(
    *, policy: OdooOwnershipRepoPolicy, relative_path: str, text: str
) -> tuple[OdooOwnershipFinding, ...]:
    findings: list[OdooOwnershipFinding] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped_line = line.strip()
        if not stripped_line:
            continue
        for rule in _RULES:
            if policy.family not in rule.families:
                continue
            if not _matches_any(relative_path, rule.path_globs):
                continue
            if _matches_any(relative_path, rule.allowed_path_globs):
                continue
            if any(pattern.search(line) for pattern in rule.allowed_line_patterns):
                continue
            if rule.pattern.search(line):
                findings.append(
                    OdooOwnershipFinding(
                        repository=policy.repository,
                        family=policy.family,
                        path=relative_path,
                        line=line_number,
                        rule_id=rule.rule_id,
                        severity=rule.severity,
                        message=rule.message,
                        evidence=stripped_line[:240],
                    )
                )
    return tuple(findings)


def _iter_scannable_files(repo_root: Path) -> Iterable[Path]:
    for path in sorted(repo_root.rglob("*")):
        if not path.is_file():
            continue
        if any(part in _IGNORED_PARTS for part in path.relative_to(repo_root).parts):
            continue
        if path.suffix not in _TEXT_FILE_SUFFIXES:
            continue
        yield path


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in patterns)
