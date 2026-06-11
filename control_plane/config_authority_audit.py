from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import cast


MAX_SCANNED_FILE_BYTES = 1_000_000
MAX_EVIDENCE_VALUE_LENGTH = 96
HASH_VERSION = "config-authority-audit-v1"

ALLOW_REASON_DOCS_EXAMPLE = "docs_example"
ALLOW_REASON_TEST_FIXTURE = "test_fixture"
ALLOW_REASON_SCHEMA_ONLY = "schema_only"
ALLOW_REASON_LAUNCHPLANE_SELF_BOOTSTRAP = "launchplane_self_bootstrap"
ALLOW_REASON_THIN_CONNECTOR_INPUT = "thin_connector_input"
ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT = "operator_supplied_runtime_input"
ALLOW_REASON_PRODUCT_OWNED_ADDON = "product_owned_addon"
ALLOW_REASON_REPO_METADATA_ERGONOMICS = "repo_metadata_ergonomics"

SCAN_MODES = ("full-audit", "changed-files-gate")
OUTPUT_FORMATS = ("json", "markdown")

SECRET_SHAPED_KEY_PARTS = frozenset(("PASSWORD", "TOKEN", "SECRET", "KEY"))
BOOTSTRAP_ENV_KEYS = frozenset(
    (
        "DOCKER_IMAGE_REFERENCE",
        "LAUNCHPLANE_APP_ROOT",
        "LAUNCHPLANE_DATABASE_URL",
        "LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET",
        "LAUNCHPLANE_EVERY_CODE_WORKER_TOKEN",
        "LAUNCHPLANE_LOCAL_ADMIN_SUBJECT",
        "LAUNCHPLANE_LOCAL_ADMIN_TOKEN",
        "LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL",
        "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT",
        "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN",
        "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL",
        "LAUNCHPLANE_MASTER_ENCRYPTION_KEY",
        "LAUNCHPLANE_POLICY_B64",
        "LAUNCHPLANE_POLICY_FILE",
        "LAUNCHPLANE_POLICY_TOML",
        "LAUNCHPLANE_SERVICE_AUDIENCE",
        "LAUNCHPLANE_SERVICE_HOST",
        "LAUNCHPLANE_SERVICE_PORT",
        "LAUNCHPLANE_STATE_DIR",
        "LAUNCHPLANE_TERMINAL_AGENT_READ_TOKEN",
        "LAUNCHPLANE_TERMINAL_AGENT_SUBJECT",
        "LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL",
    )
)

RUNTIME_IDENTITY_KEY_PATTERNS = tuple(
    re.compile(pattern)
    for pattern in (
        r"(^|_)(AUTHZ|OPERATOR|TENANT|PRODUCT|CONTEXT|INSTANCE|LANE|DOMAIN|TARGET|REPO|REPOSITORY|BRANCH|ENVIRONMENT)($|_)",
        r"(^|_)(BASE_URL|HEALTH_URL|PUBLIC_URL|PREVIEW_URL)($|_)",
        r"(^|_)(DOKPLOY|NPMPLUS|ODOO|GITHUB)($|_)",
    )
)
URL_PATTERN = re.compile(r"https?://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
OWNER_REPO_PATTERN = re.compile(r"\b[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\b")
PROVIDER_TARGET_PATTERN = re.compile(r"\b(?:dokploy|npmplus|provider)[-_]?[A-Za-z0-9_.-]+\b", re.I)
CATALOG_KEY_PATTERN = re.compile(
    r"(?:^|_)(DEFAULT|POLICIES|POLICY|CATALOG|REGISTRY|TARGETS|DOMAINS|LANES|ENVIRONMENTS|REPOSITORIES|REPOS)(?:_|$)",
    re.I,
)
SEMANTIC_FIELD_PATTERN = re.compile(
    r"(?:^|[._\[])(repository|repo|product|tenant|context|instance|branch|domain|lane|target|target_id|provider|operator|subject|authz)(?:$|[.\]])",
    re.I,
)
ENV_ASSIGNMENT_PATTERN = re.compile(
    r"^(?:export\s+)?(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?P<value>.+?)\s*$"
)
SHELL_ENV_PATTERN = re.compile(
    r"\b(?P<key>[A-Z][A-Z0-9_]{2,})\s*=\s*(?P<value>\"[^\"]*\"|'[^']*'|[^\s#]+)"
)
YAML_SCALAR_PATTERN = re.compile(r"^\s*(?P<key>[A-Za-z0-9_.-]+)\s*:\s*(?P<value>.+?)\s*$")
GITHUB_EXPRESSION_PATTERN = re.compile(r"^\$\{\{\s*(?P<body>[^}]+?)\s*\}\}$")
GITHUB_CONTEXT_REFERENCE_PATTERN = re.compile(
    r"^(?:env|github|inputs|matrix|needs|secrets|steps|vars)\.[A-Za-z0-9_.-]+$"
)
GITHUB_ROUTE_PATH_FORWARDING_PATTERN = re.compile(
    r"^(?:inputs\.[A-Za-z0-9_.-]+|steps\.[A-Za-z0-9_-]+\.outputs\.[A-Za-z0-9_.-]+)$"
)
GITHUB_INPUT_REFERENCE_PATTERN = re.compile(r"^inputs\.[A-Za-z0-9_.-]+$")
WORKFLOW_RUNTIME_AUTHORITY_KEYS = frozenset(
    ("GITHUB_TOKEN", "ID_TOKEN", "LAUNCHPLANE_PRODUCT", "LAUNCHPLANE_URL")
)
WORKFLOW_OPERATOR_INPUT_VALUE_KEYS = frozenset(
    (
        "APP_NAME",
        "CONTEXT",
        "CONFIRMATION",
        "DEPLOY_TIMEOUT_SECONDS",
        "DESCRIPTION",
        "DOMAIN",
        "COMPOSE_PATH",
        "EDGE_ENDPOINT_KEY",
        "ENVIRONMENT_ID",
        "ENVIRONMENT_NAME",
        "HEALTHCHECK_PATH",
        "INSTANCE",
        "MODE",
        "OPERATION",
        "PRODUCT",
        "PROJECT_ID",
        "PROJECT_NAME",
        "REASON",
        "REPOSITORY",
        "RUNTIME_PORT",
        "SERVER_ID",
        "SOURCE_GIT_REF",
        "SOURCE_TYPE",
        "BASE_BRANCH",
        "TARGET_ID",
        "TARGET_NAME",
        "TARGET_TYPE",
    )
)
INGRESS_ROUTE_WORKFLOW_PATHS = frozenset(
    (
        ".github/workflows/ingress-route-apply.yml",
        ".github/workflows/ingress-route-canary-apply.yml",
        ".github/workflows/ingress-route-dry-run.yml",
    )
)
IGNORED_YAML_SCALAR_KEYS = frozenset(("description", "id", "name", "run", "uses"))
YAML_BLOCK_SCALAR_OPENERS = frozenset(("|", "|-", "|+", ">", ">-", ">+"))
DOCKER_ENV_PATTERN = re.compile(r"^\s*(?:ENV|ARG)\s+(?P<assignment>.+?)\s*$", re.I)

IGNORED_DIR_NAMES = frozenset(
    (
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
        "dist",
        "build",
    )
)
TEXT_SCAN_SUFFIXES = frozenset(
    (
        "",
        ".Dockerfile",
        ".env",
        ".example",
        ".ini",
        ".json",
        ".just",
        ".make",
        ".md",
        ".py",
        ".sh",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
    )
)
SCRIPT_NAMES = frozenset(("Dockerfile", "Makefile", "Justfile", "justfile", "makefile"))
SKIPPED_DEPENDENCY_MANIFEST_NAMES = frozenset(
    (
        "bun.lock",
        "package-lock.json",
        "pnpm-lock.yaml",
        "uv.lock",
        "yarn.lock",
    )
)


@dataclass(frozen=True)
class AuditSourceFile:
    path: Path
    relative_path: str
    size: int
    mtime_ns: int
    sha256: str
    git_status: str
    head_blob_sha: str
    index_blob_sha: str
    worktree_sha256: str

    def as_payload(self) -> dict[str, object]:
        return {
            "path": self.relative_path,
            "size": self.size,
            "mtime_ns": self.mtime_ns,
            "sha256": self.sha256,
            "git_status": self.git_status,
            "head_blob_sha": self.head_blob_sha,
            "index_blob_sha": self.index_blob_sha,
            "worktree_sha256": self.worktree_sha256,
        }


@dataclass(frozen=True)
class ConfigAuthorityFinding:
    finding_id: str
    path: str
    line: int
    rule_id: str
    severity: str
    key: str
    value_hash: str
    evidence: str
    classification: str
    allow_reason: str
    parser: str
    git_status: str

    def as_payload(self) -> dict[str, object]:
        return {
            "finding_id": self.finding_id,
            "path": self.path,
            "line": self.line,
            "rule_id": self.rule_id,
            "severity": self.severity,
            "key": self.key,
            "value_hash": self.value_hash,
            "evidence": self.evidence,
            "classification": self.classification,
            "allow_reason": self.allow_reason,
            "parser": self.parser,
            "git_status": self.git_status,
        }


@dataclass(frozen=True)
class CoverageGap:
    path: str
    reason: str
    detail: str

    def as_payload(self) -> dict[str, object]:
        return {"path": self.path, "reason": self.reason, "detail": self.detail}


def build_config_authority_audit(
    *,
    control_plane_root: Path,
    mode: str = "full-audit",
    include_untracked: bool = False,
    include_ignored: bool = False,
    paths: Sequence[Path] = (),
) -> dict[str, object]:
    if mode not in SCAN_MODES:
        raise ValueError(f"Unsupported config authority audit mode: {mode}")

    root = control_plane_root.resolve()
    repo_metadata = _repo_metadata(root)
    git_file_state = _git_file_state(root)
    source_files, coverage_gaps = _discover_source_files(
        root=root,
        mode=mode,
        include_untracked=include_untracked,
        include_ignored=include_ignored,
        paths=paths,
        git_file_state=git_file_state,
    )
    findings: list[ConfigAuthorityFinding] = []
    raw_findings: list[dict[str, object]] = []
    for source_file in source_files:
        file_findings, file_gaps = _scan_source_file(source_file)
        findings.extend(file_findings)
        coverage_gaps.extend(file_gaps)
        raw_findings.extend(_raw_finding_payload(finding) for finding in file_findings)

    findings = sorted(findings, key=lambda item: (item.path, item.line, item.rule_id, item.key))
    finding_payloads = [finding.as_payload() for finding in findings]
    source_payloads = [source_file.as_payload() for source_file in source_files]
    payload: dict[str, object] = {
        "status": "ok",
        "mode": mode,
        "control_plane_root": str(root),
        "repo": repo_metadata,
        "scanner": {
            "version": HASH_VERSION,
            "include_untracked": include_untracked,
            "include_ignored": include_ignored,
            "max_scanned_file_bytes": MAX_SCANNED_FILE_BYTES,
        },
        "coverage": {
            "source_file_count": len(source_files),
            "finding_count": len(findings),
            "coverage_gap_count": len(coverage_gaps),
            "gaps": [gap.as_payload() for gap in coverage_gaps],
        },
        "hashes": {
            "input_set_hash": _stable_hash(source_payloads),
            "finding_set_hash": _stable_hash(finding_payloads),
        },
        "source_files": source_payloads,
        "findings": finding_payloads,
    }
    if raw_findings:
        payload["raw_finding_count"] = len(raw_findings)
    return payload


def render_config_authority_markdown(payload: Mapping[str, object]) -> str:
    coverage = _mapping_payload(payload.get("coverage"))
    hashes = _mapping_payload(payload.get("hashes"))
    findings = _list_payload(payload.get("findings"))
    gaps = _list_payload(coverage.get("gaps"))
    lines = [
        "# Config Authority Audit",
        "",
        f"- Mode: `{payload.get('mode', '')}`",
        f"- Source files: `{coverage.get('source_file_count', 0)}`",
        f"- Findings: `{coverage.get('finding_count', 0)}`",
        f"- Coverage gaps: `{coverage.get('coverage_gap_count', 0)}`",
        f"- Input set hash: `{hashes.get('input_set_hash', '')}`",
        f"- Finding set hash: `{hashes.get('finding_set_hash', '')}`",
        "",
        "## Findings",
        "",
    ]
    if not findings:
        lines.append("No findings.")
    else:
        lines.append("| ID | Severity | Path | Key | Classification | Allow Reason |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for finding in findings:
            if not isinstance(finding, dict):
                continue
            path = f"{finding.get('path', '')}:{finding.get('line', '')}"
            lines.append(
                "| {finding_id} | {severity} | `{path}` | `{key}` | {classification} | {allow_reason} |".format(
                    finding_id=finding.get("finding_id", ""),
                    severity=finding.get("severity", ""),
                    path=path,
                    key=finding.get("key", ""),
                    classification=finding.get("classification", ""),
                    allow_reason=finding.get("allow_reason", ""),
                )
            )
    lines.extend(("", "## Coverage Gaps", ""))
    if not gaps:
        lines.append("No coverage gaps.")
    else:
        lines.append("| Path | Reason | Detail |")
        lines.append("| --- | --- | --- |")
        for gap in gaps:
            if not isinstance(gap, dict):
                continue
            lines.append(
                "| `{path}` | {reason} | {detail} |".format(
                    path=gap.get("path", ""),
                    reason=gap.get("reason", ""),
                    detail=str(gap.get("detail", "")).replace("|", "\\|"),
                )
            )
    return "\n".join(lines) + "\n"


def _discover_source_files(
    *,
    root: Path,
    mode: str,
    include_untracked: bool,
    include_ignored: bool,
    paths: Sequence[Path],
    git_file_state: Mapping[str, Mapping[str, str]],
) -> tuple[list[AuditSourceFile], list[CoverageGap]]:
    candidate_paths = _candidate_paths(
        root=root,
        mode=mode,
        include_untracked=include_untracked,
        include_ignored=include_ignored,
        paths=paths,
    )
    source_files: list[AuditSourceFile] = []
    coverage_gaps: list[CoverageGap] = []
    for path in candidate_paths:
        relative_path = _relative_path(root=root, path=path)
        if not _is_text_scan_candidate(path):
            coverage_gaps.append(
                CoverageGap(
                    path=relative_path,
                    reason="unscanned_file_class",
                    detail="File extension or name is not in the MVP scanner surface.",
                )
            )
            continue
        if path.name in SKIPPED_DEPENDENCY_MANIFEST_NAMES:
            coverage_gaps.append(
                CoverageGap(
                    path=relative_path,
                    reason="skipped_dependency_manifest",
                    detail="Dependency lockfiles are not config-authority surfaces in the MVP scanner.",
                )
            )
            continue
        try:
            stat = path.stat()
        except OSError as error:
            coverage_gaps.append(
                CoverageGap(path=relative_path, reason="unreadable_file", detail=str(error))
            )
            continue
        if stat.st_size > MAX_SCANNED_FILE_BYTES:
            coverage_gaps.append(
                CoverageGap(
                    path=relative_path,
                    reason="skipped_large_file",
                    detail=f"File is {stat.st_size} bytes; limit is {MAX_SCANNED_FILE_BYTES} bytes.",
                )
            )
            continue
        try:
            content = path.read_bytes()
        except OSError as error:
            coverage_gaps.append(
                CoverageGap(path=relative_path, reason="unreadable_file", detail=str(error))
            )
            continue
        if _looks_binary(content):
            coverage_gaps.append(
                CoverageGap(
                    path=relative_path,
                    reason="skipped_binary_file",
                    detail="File contains NUL bytes or cannot be scanned as text.",
                )
            )
            continue
        git_state = git_file_state.get(relative_path, {})
        sha256 = hashlib.sha256(content).hexdigest()
        source_files.append(
            AuditSourceFile(
                path=path,
                relative_path=relative_path,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                sha256=sha256,
                git_status=str(git_state.get("git_status") or "untracked"),
                head_blob_sha=str(git_state.get("head_blob_sha") or ""),
                index_blob_sha=str(git_state.get("index_blob_sha") or ""),
                worktree_sha256=sha256,
            )
        )
    return source_files, coverage_gaps


def _candidate_paths(
    *,
    root: Path,
    mode: str,
    include_untracked: bool,
    include_ignored: bool,
    paths: Sequence[Path],
) -> list[Path]:
    if paths:
        return sorted(
            {
                _resolve_scan_path(root=root, path=path)
                for path in paths
                if _resolve_scan_path(root=root, path=path).is_file()
            }
        )
    if mode == "changed-files-gate":
        return _git_changed_files(root)
    if include_ignored:
        discovered: list[Path] = []
        for directory, dir_names, file_names in os.walk(root):
            dir_names[:] = [
                name
                for name in dir_names
                if name not in IGNORED_DIR_NAMES and (include_ignored or name != ".code")
            ]
            for file_name in file_names:
                discovered.append(Path(directory) / file_name)
        return sorted(discovered)
    discovered_paths = [root / relative_path for relative_path in _git_tracked_relative_paths(root)]
    if include_untracked:
        discovered_paths.extend(
            root / relative_path for relative_path in _git_untracked_relative_paths(root)
        )
    return sorted(discovered_paths)


def _scan_source_file(
    source_file: AuditSourceFile,
) -> tuple[list[ConfigAuthorityFinding], list[CoverageGap]]:
    try:
        text = source_file.path.read_text(encoding="utf-8")
    except UnicodeDecodeError as error:
        return [], [
            CoverageGap(
                path=source_file.relative_path,
                reason="decode_failure",
                detail=str(error),
            )
        ]
    parser = _parser_name(source_file.path)
    candidates: list[tuple[int, str, object]] = []
    coverage_gaps: list[CoverageGap] = []
    if parser == "python_ast":
        parsed_candidates, parse_error = _python_candidates(source_file.relative_path, text)
        candidates.extend(parsed_candidates)
        if parse_error:
            coverage_gaps.append(
                CoverageGap(source_file.relative_path, "parse_failure", parse_error)
            )
    elif parser == "json":
        parsed_candidates, parse_error = _json_candidates(text)
        candidates.extend(parsed_candidates)
        if parse_error:
            coverage_gaps.append(
                CoverageGap(source_file.relative_path, "parse_failure", parse_error)
            )
    elif parser == "toml":
        parsed_candidates, parse_error = _toml_candidates(text)
        candidates.extend(parsed_candidates)
        if parse_error:
            coverage_gaps.append(
                CoverageGap(source_file.relative_path, "parse_failure", parse_error)
            )
    elif parser == "yaml_line_scan":
        candidates.extend(_yaml_line_candidates(text))
        coverage_gaps.append(
            CoverageGap(
                source_file.relative_path,
                "parser_limitation",
                "YAML scanned line-by-line because no structured YAML parser is available.",
            )
        )
    elif parser == "env_line_scan":
        candidates.extend(_env_line_candidates(text))
    elif parser == "dockerfile_line_scan":
        candidates.extend(_dockerfile_line_candidates(text))
    else:
        candidates.extend(_script_line_candidates(text))

    findings = [
        _build_finding(
            source_file=source_file,
            line=line,
            key=key,
            value=value,
            parser=parser,
        )
        for line, key, value in candidates
        if _candidate_is_interesting(path=source_file.relative_path, key=key, value=value)
    ]
    return findings, coverage_gaps


def _python_candidates(relative_path: str, text: str) -> tuple[list[tuple[int, str, object]], str]:
    try:
        tree = ast.parse(text)
    except SyntaxError as error:
        return [], str(error)
    dataclass_fields = _python_dataclass_fields(tree)
    candidates: list[tuple[int, str, object]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            value = _literal_value(node.value)
            for target in node.targets:
                key = _assignment_target_name(target)
                if key and value is not None:
                    candidates.append((node.lineno, key, value))
                if key:
                    candidates.extend(
                        _python_semantic_value_candidates(
                            node.value,
                            base_key=key,
                            dataclass_fields=dataclass_fields,
                        )
                    )
        elif isinstance(node, ast.AnnAssign):
            value = _literal_value(node.value) if node.value is not None else None
            key = _assignment_target_name(node.target)
            if key and value is not None:
                candidates.append((node.lineno, key, value))
            if key and node.value is not None:
                candidates.extend(
                    _python_semantic_value_candidates(
                        node.value,
                        base_key=key,
                        dataclass_fields=dataclass_fields,
                    )
                )
        elif isinstance(node, ast.Call):
            candidates.extend(_python_call_candidates(node))
    return candidates, ""


def _python_dataclass_fields(tree: ast.Module) -> dict[str, tuple[str, ...]]:
    fields: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or not _is_dataclass_class(node):
            continue
        class_fields: list[str] = []
        for statement in node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                class_fields.append(statement.target.id)
        fields[node.name] = tuple(class_fields)
    return fields


def _is_dataclass_class(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        if _call_name(decorator) == "dataclass":
            return True
        if isinstance(decorator, ast.Call) and _call_name(decorator.func) == "dataclass":
            return True
    return False


def _python_semantic_value_candidates(
    node: ast.AST,
    *,
    base_key: str,
    dataclass_fields: Mapping[str, tuple[str, ...]],
) -> list[tuple[int, str, object]]:
    candidates: list[tuple[int, str, object]] = []
    literal_value = _literal_value(node)
    if literal_value is not None:
        for child_key, value in _flatten_python_literal(literal_value, prefix=base_key):
            if child_key != base_key or _semantic_path_has_context(base_key):
                candidates.append((getattr(node, "lineno", 0), child_key, value))
        return candidates
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        for index, item in enumerate(node.elts):
            candidates.extend(
                _python_semantic_value_candidates(
                    item,
                    base_key=f"{base_key}[{index}]",
                    dataclass_fields=dataclass_fields,
                )
            )
        return candidates
    if isinstance(node, ast.Dict):
        for key_node, value_node in zip(node.keys, node.values, strict=False):
            key_value = _literal_value(key_node)
            child_key = (
                f"{base_key}.{key_value}" if key_value is not None else f"{base_key}.<dynamic>"
            )
            candidates.extend(
                _python_semantic_value_candidates(
                    value_node,
                    base_key=child_key,
                    dataclass_fields=dataclass_fields,
                )
            )
        return candidates
    if isinstance(node, ast.Call):
        call_name = _call_name(node.func)
        call_leaf_name = call_name.rsplit(".", 1)[-1]
        field_names = dataclass_fields.get(call_name) or dataclass_fields.get(call_leaf_name, ())
        if not field_names:
            return candidates
        for index, argument in enumerate(node.args):
            field_name = field_names[index] if index < len(field_names) else f"arg{index}"
            candidates.extend(
                _python_semantic_value_candidates(
                    argument,
                    base_key=f"{base_key}.{call_name}.{field_name}",
                    dataclass_fields=dataclass_fields,
                )
            )
        for keyword in node.keywords:
            if keyword.arg is None:
                continue
            candidates.extend(
                _python_semantic_value_candidates(
                    keyword.value,
                    base_key=f"{base_key}.{call_name}.{keyword.arg}",
                    dataclass_fields=dataclass_fields,
                )
            )
    return candidates


def _flatten_python_literal(value: object, prefix: str) -> Iterable[tuple[str, object]]:
    if isinstance(value, dict):
        for key, item in value.items():
            child_prefix = f"{prefix}.{key}"
            yield from _flatten_python_literal(item, child_prefix)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for index, item in enumerate(value):
            child_prefix = f"{prefix}[{index}]"
            yield from _flatten_python_literal(item, child_prefix)
    else:
        yield prefix, value


def _semantic_path_has_context(key: str) -> bool:
    return "." in key or "[" in key


def _python_call_candidates(node: ast.Call) -> list[tuple[int, str, object]]:
    candidates: list[tuple[int, str, object]] = []
    function_name = _call_name(node.func)
    if function_name in {"click.option", "option"}:
        option_names = [
            value
            for value in (_literal_value(argument) for argument in node.args)
            if isinstance(value, str)
        ]
        option_key = next((name for name in option_names if name.startswith("--")), "click.option")
        for keyword in node.keywords:
            if keyword.arg in {"default", "envvar", "required"}:
                value = _literal_value(keyword.value)
                if value is not None:
                    candidates.append((node.lineno, f"{option_key}.{keyword.arg}", value))
    return candidates


def _json_candidates(text: str) -> tuple[list[tuple[int, str, object]], str]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        return [], str(error)
    return [(1, key, value) for key, value in _flatten_mapping(parsed)], ""


def _toml_candidates(text: str) -> tuple[list[tuple[int, str, object]], str]:
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        return [], str(error)
    return [(1, key, value) for key, value in _flatten_mapping(parsed)], ""


def _yaml_line_candidates(text: str) -> list[tuple[int, str, object]]:
    candidates: list[tuple[int, str, object]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("-"):
            continue
        match = YAML_SCALAR_PATTERN.match(line)
        if match is None:
            continue
        if match.group("key") in IGNORED_YAML_SCALAR_KEYS:
            continue
        value = _strip_inline_comment(match.group("value")).strip()
        if value and value not in YAML_BLOCK_SCALAR_OPENERS:
            candidates.append((index, match.group("key"), _unquote(value)))
    return candidates


def _env_line_candidates(text: str) -> list[tuple[int, str, object]]:
    candidates: list[tuple[int, str, object]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ENV_ASSIGNMENT_PATTERN.match(stripped)
        if match is not None:
            candidates.append((index, match.group("key"), _unquote(match.group("value"))))
    return candidates


def _dockerfile_line_candidates(text: str) -> list[tuple[int, str, object]]:
    candidates: list[tuple[int, str, object]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        match = DOCKER_ENV_PATTERN.match(line)
        if match is None:
            continue
        assignment = match.group("assignment")
        candidates.extend((index, key, value) for key, value in _assignment_tokens(assignment))
    return candidates


def _script_line_candidates(text: str) -> list[tuple[int, str, object]]:
    candidates: list[tuple[int, str, object]] = []
    for index, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        candidates.extend(
            (index, match.group("key"), _unquote(match.group("value")))
            for match in SHELL_ENV_PATTERN.finditer(line)
        )
        for url in URL_PATTERN.findall(line):
            candidates.append((index, "url", url))
        for repo in OWNER_REPO_PATTERN.findall(line):
            candidates.append((index, "repository", repo))
    return candidates


def _build_finding(
    *,
    source_file: AuditSourceFile,
    line: int,
    key: str,
    value: object,
    parser: str,
) -> ConfigAuthorityFinding:
    rule_id = _rule_id(key=key, value=value)
    allow_reason = _allow_reason(path=source_file.relative_path, key=key, value=value)
    classification = "allowed" if allow_reason else "needs_classification"
    severity = "info" if allow_reason else _severity(rule_id=rule_id, key=key)
    evidence = _redacted_evidence(key=key, value=value)
    value_hash = _stable_hash(value)
    finding_id = _finding_id(
        path=source_file.relative_path,
        line=line,
        rule_id=rule_id,
        key=key,
        value_hash=value_hash,
    )
    return ConfigAuthorityFinding(
        finding_id=finding_id,
        path=source_file.relative_path,
        line=line,
        rule_id=rule_id,
        severity=severity,
        key=key,
        value_hash=value_hash,
        evidence=evidence,
        classification=classification,
        allow_reason=allow_reason,
        parser=parser,
        git_status=source_file.git_status,
    )


def _candidate_is_interesting(*, path: str, key: str, value: object) -> bool:
    normalized = path.replace("\\", "/")
    key_text = key.upper().replace(".", "_").replace("-", "_")
    value_text = _string_value(value)
    if _is_click_option_metadata_key(key):
        return True
    if _is_repo_metadata_ergonomics_key(key):
        return True
    if key_text in WORKFLOW_RUNTIME_AUTHORITY_KEYS:
        return True
    if normalized.startswith(".github/workflows/") and (
        _is_launchplane_service_route_path(key=key, value=value)
        or _is_workflow_mechanic_key_value(key=key, value=value)
        or _is_workflow_operator_input_value(key=key, value=value)
        or _is_workflow_operator_variable_forward(key=key, value=value)
    ):
        return True
    if not value_text.strip():
        return False
    if key_text in BOOTSTRAP_ENV_KEYS:
        return True
    if any(pattern.search(key_text) for pattern in RUNTIME_IDENTITY_KEY_PATTERNS):
        return True
    if CATALOG_KEY_PATTERN.search(key) and SEMANTIC_FIELD_PATTERN.search(key):
        return True
    if any(part in key_text.split("_") for part in SECRET_SHAPED_KEY_PARTS):
        return True
    return bool(
        URL_PATTERN.search(value_text)
        or OWNER_REPO_PATTERN.search(value_text)
        or PROVIDER_TARGET_PATTERN.search(value_text)
    )


def _rule_id(*, key: str, value: object) -> str:
    leaf_text = _semantic_leaf_text(key)
    value_text = _string_value(value)
    if any(part in leaf_text.split("_") for part in SECRET_SHAPED_KEY_PARTS):
        return "secret_binding_identity"
    if URL_PATTERN.search(value_text) or "DOMAIN" in leaf_text or "URL" in leaf_text:
        return "domain_or_url_authority"
    if OWNER_REPO_PATTERN.search(value_text) or "REPO" in leaf_text or "REPOSITORY" in leaf_text:
        return "repository_authority"
    if "AUTHZ" in leaf_text or "OPERATOR" in leaf_text or "SUBJECT" in leaf_text:
        return "authz_or_operator_authority"
    if (
        "TARGET" in leaf_text
        or "PROVIDER" in leaf_text
        or PROVIDER_TARGET_PATTERN.search(value_text)
    ):
        return "provider_target_authority"
    return "runtime_config_authority"


def _severity(*, rule_id: str, key: str) -> str:
    if rule_id in {"secret_binding_identity", "authz_or_operator_authority"}:
        return "high"
    if key.startswith("--"):
        return "medium"
    return "medium"


def _allow_reason(*, path: str, key: str, value: object) -> str:
    normalized = path.replace("\\", "/")
    key_text = key.upper().replace(".", "_").replace("-", "_")
    if normalized.startswith("docs/") or normalized in {"README.md", "AGENTS.md", "handoff.md"}:
        return ALLOW_REASON_DOCS_EXAMPLE
    if normalized.startswith("tests/") or "/test" in normalized:
        return ALLOW_REASON_TEST_FIXTURE
    if normalized.startswith("addons/") or "/addons/" in normalized:
        return ALLOW_REASON_PRODUCT_OWNED_ADDON
    if normalized == ".github/github.json" and _is_repo_metadata_ergonomics_key(key):
        return ALLOW_REASON_REPO_METADATA_ERGONOMICS
    if normalized.endswith(".py") and (
        key_text.startswith("ALLOW_REASON_")
        or key_text.endswith(("FIELDS", "SCHEMA", "MODEL_CONFIG"))
        or "PATH_GLOBS" in key_text
    ):
        return ALLOW_REASON_SCHEMA_ONLY
    if key_text in BOOTSTRAP_ENV_KEYS:
        return ALLOW_REASON_LAUNCHPLANE_SELF_BOOTSTRAP
    if normalized.startswith(".github/workflows/") and _is_launchplane_public_url_reference(
        key=key,
        value=value,
    ):
        return ALLOW_REASON_LAUNCHPLANE_SELF_BOOTSTRAP
    if _is_launchplane_self_management_product_reference(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_LAUNCHPLANE_SELF_BOOTSTRAP
    if normalized.startswith(".github/workflows/") and _is_workflow_mechanic_key_value(
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_operator_input_value(
        key=key,
        value=value,
    ):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_operator_variable_forward(
        key=key,
        value=value,
    ):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    if normalized.startswith(".github/workflows/") and _is_workflow_operator_array_forward(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    if normalized.startswith(".github/workflows/") and _is_ingress_route_option_literal(
        path=normalized,
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if (
        normalized.startswith(".github/workflows/")
        and not _is_workflow_runtime_authority_key(key)
        and not _is_workflow_operator_input_key(key)
        and not _is_route_path_key(key)
        and _is_github_context_reference(value)
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if normalized.startswith(".github/workflows/") and _is_launchplane_service_route_path(
        key=key,
        value=value,
    ):
        return ALLOW_REASON_THIN_CONNECTOR_INPUT
    if key_text.startswith("--") and key_text.endswith(("REQUIRED", "DEFAULT")):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    if _is_click_option_metadata_key(key):
        return ALLOW_REASON_OPERATOR_SUPPLIED_RUNTIME_INPUT
    return ""


def _is_github_context_reference(value: object) -> bool:
    match = GITHUB_EXPRESSION_PATTERN.match(_string_value(value).strip())
    if match is None:
        return False
    return bool(GITHUB_CONTEXT_REFERENCE_PATTERN.match(match.group("body").strip()))


def _is_workflow_runtime_authority_key(key: str) -> bool:
    key_text = key.upper().replace(".", "_").replace("-", "_")
    return key_text in WORKFLOW_RUNTIME_AUTHORITY_KEYS


def _is_launchplane_public_url_reference(*, key: str, value: object) -> bool:
    key_text = key.upper().replace(".", "_").replace("-", "_")
    value_text = _string_value(value).strip()
    if key == "LAUNCHPLANE_URL":
        return value_text == "${{ vars.LAUNCHPLANE_PUBLIC_URL }}"
    return key_text == "LAUNCHPLANE_URL" and value_text == "${{ env.LAUNCHPLANE_URL }}"


def _is_launchplane_self_management_product_reference(
    *, path: str, key: str, value: object
) -> bool:
    value_text = _string_value(value).strip().rstrip(",")
    return (
        path == ".github/workflows/dokploy-target-setup.yml"
        and key == "product"
        and value_text in {'"launchplane"', "launchplane"}
    )


def _is_workflow_operator_input_value(*, key: str, value: object) -> bool:
    if not _is_workflow_operator_input_key(key):
        return False
    match = GITHUB_EXPRESSION_PATTERN.match(_string_value(value).strip())
    if match is None:
        return False
    return bool(GITHUB_INPUT_REFERENCE_PATTERN.match(match.group("body").strip()))


def _is_workflow_operator_input_key(key: str) -> bool:
    key_text = key.upper().replace(".", "_").replace("-", "_")
    return key_text in WORKFLOW_OPERATOR_INPUT_VALUE_KEYS


def _is_workflow_operator_variable_forward(*, key: str, value: object) -> bool:
    if not _is_workflow_operator_input_key(key):
        return False
    key_text = key.upper().replace(".", "_").replace("-", "_")
    value_text = _string_value(value).strip().rstrip(",")
    return value_text == f"${key_text.lower()}"


def _is_workflow_operator_array_forward(*, path: str, key: str, value: object) -> bool:
    if path not in INGRESS_ROUTE_WORKFLOW_PATHS:
        return False
    key_text = key.upper().replace(".", "_").replace("-", "_")
    value_text = _string_value(value).strip().rstrip(",").replace(" ", "")
    return key_text == "DOMAIN_NAMES" and value_text == "[$domain]"


def _is_ingress_route_option_literal(*, path: str, key: str, value: object) -> bool:
    if path not in INGRESS_ROUTE_WORKFLOW_PATHS:
        return False
    key_text = key.lower().replace("-", "_")
    value_text = _string_value(value).strip().rstrip(",")
    return key_text in {"npmplus_http3_support", "npmplus_noindex"} and value_text in {
        "false",
        "true",
    }


def _is_workflow_mechanic_key_value(*, key: str, value: object) -> bool:
    key_text = key.upper().replace(".", "_").replace("-", "_")
    value_text = _string_value(value).strip()
    if key_text == "ID_TOKEN" and value_text == "write":
        return True
    if key_text == "GROUP" and "${{ inputs." in value_text and "${{ vars." not in value_text:
        return True
    if key_text == "PATH" and re.fullmatch(r"[A-Za-z0-9_.-]+\.json", value_text):
        return True
    return False


def _is_route_path_key(key: str) -> bool:
    return key.upper().replace(".", "_").replace("-", "_") == "ROUTE_PATH"


def _is_launchplane_service_route_path(*, key: str, value: object) -> bool:
    key_text = key.upper().replace(".", "_").replace("-", "_")
    value_text = _string_value(value).strip()
    if key_text != "ROUTE_PATH":
        return False
    if value_text.startswith("/v1/") and "${{" not in value_text and " " not in value_text:
        return True
    return _is_github_route_path_forwarding_reference(value)


def _is_github_route_path_forwarding_reference(value: object) -> bool:
    match = GITHUB_EXPRESSION_PATTERN.match(_string_value(value).strip())
    if match is None:
        return False
    return bool(GITHUB_ROUTE_PATH_FORWARDING_PATTERN.match(match.group("body").strip()))


def _is_click_option_metadata_key(key: str) -> bool:
    normalized = key.lower()
    return normalized.startswith("--") and normalized.rsplit(".", 1)[-1] in {
        "default",
        "envvar",
        "required",
    }


def _is_repo_metadata_ergonomics_key(key: str) -> bool:
    return key.startswith(
        (
            "cleanup.",
            "defaultBranch",
            "deployLabels",
            "docs.",
            "githubSignals.",
            "githubSettings.",
            "importantWorkflows",
            "metadataFreshness.",
            "projectType",
            "pullRequests.",
            "qaLabels",
            "qualityGate.",
            "relatedRepos[",
        )
    )


def _repo_metadata(root: Path) -> dict[str, object]:
    return {
        "branch": _git_output(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "commit": _git_output(root, "rev-parse", "HEAD"),
        "dirty": bool(_git_output(root, "status", "--porcelain")),
    }


def _git_file_state(root: Path) -> dict[str, dict[str, str]]:
    state: dict[str, dict[str, str]] = {}
    for relative_path in _git_tracked_relative_paths(root):
        state[relative_path] = {
            "git_status": "tracked",
            "head_blob_sha": _git_output(root, "rev-parse", f"HEAD:{relative_path}"),
            "index_blob_sha": _git_output(root, "rev-parse", f":{relative_path}"),
        }
    for status, relative_path in _git_status_entries(root):
        entry = state.setdefault(relative_path, {})
        entry["git_status"] = status
        entry.setdefault("head_blob_sha", _git_output(root, "rev-parse", f"HEAD:{relative_path}"))
        entry.setdefault("index_blob_sha", _git_output(root, "rev-parse", f":{relative_path}"))
    return state


def _git_tracked_files(root: Path) -> list[Path]:
    return [root / relative_path for relative_path in _git_tracked_relative_paths(root)]


def _git_tracked_relative_paths(root: Path) -> list[str]:
    output = _git_output(root, "ls-files")
    return sorted(line for line in output.splitlines() if line.strip())


def _git_changed_files(root: Path) -> list[Path]:
    changed = set(_git_branch_changed_relative_paths(root))
    changed.update(relative_path for _, relative_path in _git_status_entries(root))
    return sorted(
        root / relative_path for relative_path in changed if (root / relative_path).is_file()
    )


def _git_branch_changed_relative_paths(root: Path) -> list[str]:
    merge_base = _git_output(root, "merge-base", "HEAD", "origin/main")
    if not merge_base:
        merge_base = _git_output(root, "merge-base", "HEAD", "main")
    if not merge_base:
        return []
    output = _git_output(root, "diff", "--name-only", "--diff-filter=ACMRT", merge_base, "HEAD")
    if not output:
        return []
    return [line for line in output.splitlines() if line]


def _git_untracked_relative_paths(root: Path) -> list[str]:
    output = _git_output(root, "ls-files", "--others", "--exclude-standard")
    return sorted(line for line in output.splitlines() if line.strip())


def _git_status_entries(root: Path) -> list[tuple[str, str]]:
    output = _git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    entries: list[tuple[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        status = line[:2].strip() or "tracked"
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries.append((status, path))
    return entries


def _git_output(root: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.rstrip("\n")


def _is_text_scan_candidate(path: Path) -> bool:
    return path.name in SCRIPT_NAMES or path.suffix in TEXT_SCAN_SUFFIXES


def _looks_binary(content: bytes) -> bool:
    return b"\x00" in content


def _parser_name(path: Path) -> str:
    if path.suffix == ".py":
        return "python_ast"
    if path.suffix == ".json":
        return "json"
    if path.suffix == ".toml":
        return "toml"
    if path.suffix in {".yaml", ".yml"}:
        return "yaml_line_scan"
    if path.name.startswith(".env") or path.suffix == ".env":
        return "env_line_scan"
    if path.name == "Dockerfile" or path.suffix == ".Dockerfile":
        return "dockerfile_line_scan"
    return "text_line_scan"


def _flatten_mapping(value: object, prefix: str = "") -> Iterable[tuple[str, object]]:
    if isinstance(value, dict):
        for key, item in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            yield from _flatten_mapping(item, child_prefix)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            child_prefix = f"{prefix}[{index}]" if prefix else f"[{index}]"
            yield from _flatten_mapping(item, child_prefix)
    else:
        yield prefix, value


def _literal_value(node: ast.AST | None) -> object | None:
    if node is None:
        return None
    try:
        return cast(object, ast.literal_eval(node))
    except (ValueError, TypeError):
        return None


def _mapping_payload(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, dict) else {}


def _list_payload(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _assignment_target_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        subscript_value = _literal_value(node.slice)
        if isinstance(subscript_value, str):
            return subscript_value
    return ""


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _call_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def _assignment_tokens(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    parts = text.split()
    index = 0
    while index < len(parts):
        part = parts[index]
        if "=" in part:
            key, value = part.split("=", 1)
            tokens.append((key, _unquote(value)))
        elif index + 1 < len(parts):
            tokens.append((part, _unquote(parts[index + 1])))
            index += 1
        index += 1
    return tokens


def _strip_inline_comment(value: str) -> str:
    quote: str | None = None
    for index, character in enumerate(value):
        if character in {"'", '"'}:
            quote = character if quote is None else None if quote == character else quote
        if character == "#" and quote is None:
            return value[:index]
    return value


def _unquote(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in {"'", '"'}:
        return stripped[1:-1]
    return stripped


def _redacted_evidence(*, key: str, value: object) -> str:
    value_text = _string_value(value)
    leaf_text = _semantic_leaf_text(key)
    full_key_text = key.upper().replace(".", "_").replace("-", "_")
    if any(part in leaf_text.split("_") for part in SECRET_SHAPED_KEY_PARTS):
        return "<redacted-secret-shaped-value>"
    if "REPOSITORY" in leaf_text or "REPO" in leaf_text:
        return "<redacted-repository>"
    if "DOMAIN" in leaf_text or "URL" in leaf_text:
        return "<redacted-url>"
    if any(
        marker in leaf_text
        for marker in (
            "PRODUCT",
            "TENANT",
            "CONTEXT",
            "INSTANCE",
            "BRANCH",
            "LANE",
            "TARGET",
            "PROVIDER",
            "OPERATOR",
            "SUBJECT",
            "AUTHZ",
        )
    ) or any(
        marker in full_key_text.split("_")
        for marker in ("PRODUCT", "TENANT", "CONTEXT", "INSTANCE", "LANE", "TARGET")
    ):
        return "<redacted-runtime-identity>"
    redacted = URL_PATTERN.sub("<redacted-url>", value_text)
    redacted = OWNER_REPO_PATTERN.sub("<redacted-repository>", redacted)
    if len(redacted) > MAX_EVIDENCE_VALUE_LENGTH:
        redacted = f"{redacted[:MAX_EVIDENCE_VALUE_LENGTH]}..."
    return redacted


def _semantic_leaf_text(key: str) -> str:
    leaf = key.rsplit(".", 1)[-1]
    leaf = leaf.split("[", 1)[0]
    return leaf.upper().replace("-", "_")


def _raw_finding_payload(finding: ConfigAuthorityFinding) -> dict[str, object]:
    payload = finding.as_payload()
    payload["raw_omitted"] = True
    return payload


def _finding_id(*, path: str, line: int, rule_id: str, key: str, value_hash: str) -> str:
    digest = _stable_hash(
        {
            "path": path,
            "line": line,
            "rule_id": rule_id,
            "key": key,
            "value_hash": value_hash,
        }
    )
    return f"caf-{digest[:12]}"


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        _json_safe_value(value),
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(HASH_VERSION.encode() + b"\0" + encoded).hexdigest()


def _string_value(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)) or value is None:
        return str(value)
    return json.dumps(_json_safe_value(value), sort_keys=True, default=str)


def _json_safe_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _json_safe_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe_value(item) for item in value]
    return value


def _relative_path(*, root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _resolve_scan_path(*, root: Path, path: Path) -> Path:
    return path if path.is_absolute() else root / path
