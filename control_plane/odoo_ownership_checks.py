from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
import fnmatch
import json
import os
from pathlib import Path
import re
import stat
from typing import Literal, get_args


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

    def __post_init__(self) -> None:
        if not self.repository.strip():
            raise ValueError("Repository root must not be empty.")
        if self.family not in get_args(OdooRepoFamily):
            raise ValueError(f"Unknown Odoo repository family: {self.family}")


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
class OdooOwnershipCoverage:
    repository: str
    family: OdooRepoFamily
    root: str
    status: Literal["complete", "unavailable"]
    scanned_file_count: int
    omitted_file_count: int


@dataclass(frozen=True)
class OdooOwnershipScanResult:
    status: Literal["pass", "fail"]
    repository_count: int
    scanned_file_count: int
    findings: tuple[OdooOwnershipFinding, ...]
    coverage: tuple[OdooOwnershipCoverage, ...]

    def to_json_payload(self) -> dict[str, object]:
        return {
            "status": self.status,
            "repository_count": self.repository_count,
            "scanned_file_count": self.scanned_file_count,
            "coverage": [asdict(entry) for entry in self.coverage],
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
    ".DS_Store",
    ".coverage",
    ".code",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
    ".tox",
}

_IGNORED_FILE_GLOBS = (".env", ".env.*", "._*")

_GLOBALLY_ALLOWED_PATH_GLOBS = (
    "addons/**",
    "**/README.md",
    "docs/**",
    "tests/**",
)

_LAUNCHPLANE_ALLOWED_LINE_PATTERNS = (
    re.compile(r"uses:\s*cbusillo/launchplane/\.github/actions/launchplane-request@"),
    re.compile(r"uses:\s*cbusillo/launchplane/\.github/workflows/reusable-odoo-"),
    re.compile(r"uses:\s*cbusillo/launchplane/\.github/workflows/reusable-product-driver-"),
    re.compile(r"LAUNCHPLANE_RUNTIME_IDENTITY_JSON"),
)

_YAML_NAME_LINE_PATTERN = re.compile(r"^\s*(?:-\s*)?name:\s*", re.IGNORECASE)

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
            r"\b(fetch|axios|requests\.(?:request|get|post|put|patch|delete)|urlopen|curl)\b"
            r".*\blaunchplane[\w-]*",
            re.IGNORECASE,
        ),
        families=("tenant", "image", "shared_addons", "devkit", "retired"),
        allowed_line_patterns=(*_LAUNCHPLANE_ALLOWED_LINE_PATTERNS, _YAML_NAME_LINE_PATTERN),
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
        allowed_line_patterns=(re.compile(r"^\s*#(?:\s|$)"),),
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
    repo_policies: Iterable[OdooOwnershipRepoPolicy],
) -> OdooOwnershipScanResult:
    findings: list[OdooOwnershipFinding] = []
    coverage: list[OdooOwnershipCoverage] = []
    resolved_workspace_root = workspace_root.expanduser().resolve()
    policies = tuple(repo_policies)
    if not policies:
        raise ValueError("Explicit repository coverage is required.")
    roots = tuple(
        (resolved_workspace_root / Path(policy.repository).expanduser()).resolve()
        for policy in policies
    )
    if len(set(roots)) != len(roots):
        raise ValueError("Each repository root must be requested exactly once.")
    for policy, repo_root in zip(policies, roots, strict=True):
        scanned = 0
        omitted = 0
        unavailable = False
        try:
            if not repo_root.is_dir():
                raise OSError("Requested repository root is missing or is not a directory.")
            for path in _iter_scannable_files(repo_root):
                relative_path = path.relative_to(repo_root).as_posix()
                if _matches_any(
                    relative_path, _GLOBALLY_ALLOWED_PATH_GLOBS + policy.allowed_path_globs
                ):
                    omitted += 1
                    continue
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as error:
                    unavailable = True
                    findings.append(
                        OdooOwnershipFinding(
                            repository=policy.repository,
                            family=policy.family,
                            path=relative_path,
                            line=0,
                            rule_id="file-unavailable",
                            severity="error",
                            message="Requested source file could not be read as UTF-8.",
                            evidence=str(error),
                        )
                    )
                    continue
                scanned += 1
                findings.extend(_scan_file(policy=policy, relative_path=relative_path, text=text))
        except OSError as error:
            unavailable = True
            findings.append(
                OdooOwnershipFinding(
                    repository=policy.repository,
                    family=policy.family,
                    path=".",
                    line=0,
                    rule_id="repository-unavailable",
                    severity="error",
                    message="Requested repository coverage could not be completed.",
                    evidence=str(error),
                )
            )
        coverage.append(
            OdooOwnershipCoverage(
                repository=policy.repository,
                family=policy.family,
                root=str(repo_root),
                status="unavailable" if unavailable else "complete",
                scanned_file_count=scanned,
                omitted_file_count=omitted,
            )
        )
    return OdooOwnershipScanResult(
        status="fail" if any(finding.severity == "error" for finding in findings) else "pass",
        repository_count=len(policies),
        scanned_file_count=sum(entry.scanned_file_count for entry in coverage),
        findings=tuple(findings),
        coverage=tuple(coverage),
    )


def render_odoo_ownership_scan_text(result: OdooOwnershipScanResult) -> str:
    lines = [
        f"status: {result.status}",
        f"repositories: {result.repository_count}",
        f"scanned_files: {result.scanned_file_count}",
        f"findings: {len(result.findings)}",
    ]
    for entry in result.coverage:
        lines.append(
            f"coverage: {entry.repository} ({entry.family}): {entry.status}; "
            f"root={entry.root}; scanned={entry.scanned_file_count}; "
            f"omitted={entry.omitted_file_count}"
        )
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
    def raise_walk_error(error: OSError) -> None:
        raise error

    for directory, subdirectories, files in os.walk(repo_root, onerror=raise_walk_error):
        subdirectories[:] = sorted(name for name in subdirectories if name not in _IGNORED_PARTS)
        for name in subdirectories:
            if (Path(directory) / name).is_symlink():
                raise OSError(f"Linked source directory is outside traversed coverage: {name}")
        for name in sorted(files):
            path = Path(directory) / name
            if (
                name not in _IGNORED_PARTS
                and not _matches_any(name, _IGNORED_FILE_GLOBS)
                and path.suffix in _TEXT_FILE_SUFFIXES
            ):
                if not stat.S_ISREG(path.stat().st_mode):
                    raise OSError(f"Source path is not a regular file: {path}")
                yield path


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(
        fnmatch.fnmatch(path, pattern)
        or (pattern.startswith("**/") and fnmatch.fnmatch(path, pattern[3:]))
        for pattern in patterns
    )
