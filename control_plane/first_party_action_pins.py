from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


LAUNCHPLANE_REQUEST_ACTION_PATH = Path(".github/actions/launchplane-request")
FIRST_PARTY_ACTION_PROVENANCE = "launchplane-request"
FULL_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
GITHUB_REPOSITORY_PATTERN = re.compile(
    r"^(?P<owner>[A-Za-z0-9_.-]+)/(?P<repository>[A-Za-z0-9_.-]+)$"
)
GITHUB_REMOTE_PATTERNS = (
    re.compile(r"^git@github\.com:(?P<repository>[^/\s]+/[^/\s]+?)(?:\.git)?$"),
    re.compile(
        r"^(?:ssh://git@|https://)github\.com/(?P<repository>[^/\s]+/[^/\s]+?)(?:\.git)?/?$"
    ),
)


class ActionPinError(ValueError):
    """Raised when first-party action pin verification cannot proceed safely."""


class PrivilegedReferenceRefused(ActionPinError):
    """Raised when action-pin tooling is pointed at an authorization trust anchor."""


@dataclass(frozen=True)
class ActionPinSite:
    path: Path
    line_number: int
    revision: str
    provenance: str | None

    @property
    def location(self) -> str:
        return f"{self.path}:{self.line_number}"

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path.as_posix(),
            "line_number": self.line_number,
            "revision": self.revision,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class ActionPinViolation:
    code: str
    message: str
    path: Path | None = None
    line_number: int | None = None

    @property
    def location(self) -> str:
        if self.path is None:
            return "repository"
        if self.line_number is None:
            return self.path.as_posix()
        return f"{self.path}:{self.line_number}"

    def as_dict(self) -> dict[str, object]:
        return {
            "code": self.code,
            "location": self.location,
            "message": self.message,
        }


@dataclass(frozen=True)
class ActionPinReport:
    action_source: str
    head_sha: str
    head_action_tree: str
    references: tuple[ActionPinSite, ...]
    violations: tuple[ActionPinViolation, ...]

    @property
    def status(self) -> str:
        return "pass" if not self.violations else "fail"

    def as_dict(self) -> dict[str, object]:
        revisions = sorted({reference.revision for reference in self.references})
        return {
            "schema_version": 1,
            "status": self.status,
            "action_source": self.action_source,
            "action_path": LAUNCHPLANE_REQUEST_ACTION_PATH.as_posix(),
            "head_sha": self.head_sha,
            "head_action_tree": self.head_action_tree,
            "reference_count": len(self.references),
            "revisions": revisions,
            "references": [reference.as_dict() for reference in self.references],
            "violations": [violation.as_dict() for violation in self.violations],
        }

    def as_summary_dict(self) -> dict[str, object]:
        return {key: value for key, value in self.as_dict().items() if key != "references"}


def discover_action_pin_sites(repo_root: Path) -> tuple[ActionPinSite, ...]:
    action_source = launchplane_request_action_source(repo_root)
    pin_line_pattern = _pin_line_pattern(action_source)
    workflow_root = repo_root / ".github" / "workflows"
    workflow_paths = sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")))
    sites: list[ActionPinSite] = []
    for path in workflow_paths:
        relative_path = path.relative_to(repo_root)
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").split("\n"),
            start=1,
        ):
            match = pin_line_pattern.match(line)
            if match is None:
                continue
            sites.append(
                ActionPinSite(
                    path=relative_path,
                    line_number=line_number,
                    revision=match.group("revision"),
                    provenance=match.group("provenance"),
                )
            )
    return tuple(sites)


def build_action_pin_report(
    repo_root: Path,
    *,
    target_revision: str = "HEAD",
) -> ActionPinReport:
    root = repo_root.resolve()
    action_source = launchplane_request_action_source(root)
    head_sha = _resolve_commit(root, target_revision)
    head_action_tree = _subtree_oid(root, head_sha, LAUNCHPLANE_REQUEST_ACTION_PATH)
    references = discover_action_pin_sites(root)
    violations: list[ActionPinViolation] = []

    if _path_is_dirty(root, LAUNCHPLANE_REQUEST_ACTION_PATH):
        violations.append(
            ActionPinViolation(
                code="action_worktree_dirty",
                message=(
                    "Commit the launchplane-request action change before checking or updating "
                    "consumer pins."
                ),
                path=LAUNCHPLANE_REQUEST_ACTION_PATH,
            )
        )
    if not references:
        violations.append(
            ActionPinViolation(
                code="action_pin_missing",
                message="No remote launchplane-request action references were found.",
            )
        )

    revisions: set[str] = set()
    revision_evidence: dict[str, tuple[str | None, bool | None, str | None]] = {}
    for reference in references:
        if FULL_SHA_PATTERN.fullmatch(reference.revision) is None:
            violations.append(
                _site_violation(
                    reference,
                    code="action_pin_invalid_revision",
                    message="launchplane-request must use a lowercase 40-character commit SHA.",
                )
            )
            continue
        revisions.add(reference.revision)
        if reference.provenance != FIRST_PARTY_ACTION_PROVENANCE:
            violations.append(
                _site_violation(
                    reference,
                    code="action_pin_invalid_provenance",
                    message=(
                        f"launchplane-request provenance must be {FIRST_PARTY_ACTION_PROVENANCE!r}."
                    ),
                )
            )
        if reference.revision not in revision_evidence:
            try:
                pinned_tree = _subtree_oid(
                    root,
                    reference.revision,
                    LAUNCHPLANE_REQUEST_ACTION_PATH,
                )
                reachable = _is_ancestor(root, reference.revision, head_sha)
            except ActionPinError as error:
                revision_evidence[reference.revision] = (None, None, str(error))
            else:
                revision_evidence[reference.revision] = (pinned_tree, reachable, None)
        evidence_tree, evidence_reachable, evidence_error = revision_evidence[reference.revision]
        if evidence_error is not None:
            violations.append(
                _site_violation(
                    reference,
                    code="action_pin_object_unavailable",
                    message=evidence_error,
                )
            )
            continue
        assert evidence_tree is not None
        assert evidence_reachable is not None
        if evidence_tree != head_action_tree:
            violations.append(
                _site_violation(
                    reference,
                    code="action_pin_content_stale",
                    message=(
                        f"Pinned action tree {evidence_tree} does not match current action tree "
                        f"{head_action_tree}."
                    ),
                )
            )
        if not evidence_reachable:
            violations.append(
                _site_violation(
                    reference,
                    code="action_pin_unreachable",
                    message=(
                        f"Pinned commit {reference.revision} is not an ancestor of {head_sha}; "
                        "squash, rebase, or force-push would leave the action reference dangling."
                    ),
                )
            )

    if len(revisions) > 1:
        violations.append(
            ActionPinViolation(
                code="action_pin_mixed_revisions",
                message=(
                    "Ordinary launchplane-request consumers must share one immutable release SHA; "
                    f"found {', '.join(sorted(revisions))}."
                ),
            )
        )

    return ActionPinReport(
        action_source=action_source,
        head_sha=head_sha,
        head_action_tree=head_action_tree,
        references=references,
        violations=tuple(violations),
    )


def update_action_pins(
    repo_root: Path,
    *,
    release_sha: str,
    source: str | None = None,
    dry_run: bool = False,
) -> tuple[Path, ...]:
    root = repo_root.resolve()
    action_source = launchplane_request_action_source(root)
    update_source = action_source if source is None else source
    _validate_update_source(update_source, expected_source=action_source)
    if FULL_SHA_PATTERN.fullmatch(release_sha) is None:
        raise ActionPinError("release_sha must be a lowercase 40-character commit SHA.")
    if _path_is_dirty(root, LAUNCHPLANE_REQUEST_ACTION_PATH):
        raise ActionPinError(
            "Commit the launchplane-request action change before sweeping consumer pins."
        )
    head_sha = _resolve_commit(root, "HEAD")
    release_tree = _subtree_oid(root, release_sha, LAUNCHPLANE_REQUEST_ACTION_PATH)
    head_tree = _subtree_oid(root, head_sha, LAUNCHPLANE_REQUEST_ACTION_PATH)
    if release_tree != head_tree:
        raise ActionPinError(
            f"Release action tree {release_tree} does not match current action tree {head_tree}."
        )
    if not _is_ancestor(root, release_sha, head_sha):
        raise ActionPinError(
            f"Release commit {release_sha} must be an ancestor of current HEAD {head_sha}."
        )

    workflow_root = root / ".github" / "workflows"
    workflow_paths = sorted((*workflow_root.glob("*.yml"), *workflow_root.glob("*.yaml")))
    pin_line_pattern = _pin_line_pattern(action_source)
    replacements: dict[Path, str] = {}
    observed_count = 0
    for path in workflow_paths:
        original = path.read_text(encoding="utf-8")
        if "\r" in original:
            raise ActionPinError(f"Refusing to normalize CRLF workflow file: {path}")
        lines = original.split("\n")
        changed = False
        for index, line in enumerate(lines):
            match = pin_line_pattern.match(line)
            if match is None:
                continue
            observed_count += 1
            replacement = (
                f"{match.group('prefix')}{update_source}@{release_sha} "
                f"# {FIRST_PARTY_ACTION_PROVENANCE}"
            )
            if replacement != line:
                lines[index] = replacement
                changed = True
        if changed:
            replacements[path] = "\n".join(lines)
    if observed_count == 0:
        raise ActionPinError("No remote launchplane-request action references were found.")

    changed_paths = tuple(path.relative_to(root) for path in sorted(replacements))
    if not dry_run:
        for path, content in replacements.items():
            temporary_path = path.with_suffix(f"{path.suffix}.tmp")
            temporary_path.write_text(content, encoding="utf-8")
            temporary_path.replace(path)
    return changed_paths


def launchplane_request_action_source(repo_root: Path) -> str:
    root = repo_root.resolve()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if GITHUB_REPOSITORY_PATTERN.fullmatch(repository) is not None:
        return f"{repository}/{LAUNCHPLANE_REQUEST_ACTION_PATH.as_posix()}"

    remote_result = _git(root, "remote", "get-url", "origin")
    if remote_result.returncode == 0:
        remote_repository = _repository_from_remote(remote_result.stdout.strip())
        if remote_repository is not None:
            return f"{remote_repository}/{LAUNCHPLANE_REQUEST_ACTION_PATH.as_posix()}"
    raise ActionPinError(
        "Could not resolve the current GitHub repository from origin or GITHUB_REPOSITORY."
    )


def _repository_from_remote(remote_url: str) -> str | None:
    for pattern in GITHUB_REMOTE_PATTERNS:
        match = pattern.fullmatch(remote_url)
        if match is not None:
            repository = match.group("repository")
            if GITHUB_REPOSITORY_PATTERN.fullmatch(repository) is not None:
                return repository
    return None


def _pin_line_pattern(action_source: str) -> re.Pattern[str]:
    return re.compile(
        r"^(?P<prefix>\s*(?:-\s+)?uses:\s*)"
        rf"(?P<source>{re.escape(action_source)})@"
        r"(?P<revision>[^#\s]+)"
        r"(?:\s+#\s*(?P<provenance>.*?))?\s*$"
    )


def _validate_update_source(source: str, *, expected_source: str) -> None:
    if "/.github/workflows/" in source:
        raise PrivilegedReferenceRefused(
            "Privileged reusable-workflow pins are authorization trust anchors and require "
            "their documented two-landing rollout."
        )
    if source != expected_source:
        raise ActionPinError(f"Unsupported first-party action source: {source!r}")


def _site_violation(
    reference: ActionPinSite,
    *,
    code: str,
    message: str,
) -> ActionPinViolation:
    return ActionPinViolation(
        code=code,
        message=message,
        path=reference.path,
        line_number=reference.line_number,
    )


def _git(repo_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ | {
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ("git", *arguments),
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _resolve_commit(repo_root: Path, revision: str) -> str:
    result = _git(repo_root, "rev-parse", "--verify", f"{revision}^{{commit}}")
    resolved = result.stdout.strip()
    if result.returncode != 0 or FULL_SHA_PATTERN.fullmatch(resolved) is None:
        raise ActionPinError(
            f"Git commit {revision!r} is unavailable locally; fetch full history before checking pins."
        )
    return resolved


def _subtree_oid(repo_root: Path, revision: str, path: Path) -> str:
    commit_sha = _resolve_commit(repo_root, revision)
    result = _git(repo_root, "rev-parse", f"{commit_sha}:{path.as_posix()}")
    object_id = result.stdout.strip()
    if result.returncode != 0 or not object_id:
        raise ActionPinError(
            f"Action subtree {path} is unavailable at commit {commit_sha}; fetch full history."
        )
    return object_id


def _is_ancestor(repo_root: Path, ancestor: str, descendant: str) -> bool:
    result = _git(repo_root, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    raise ActionPinError(
        f"Could not verify reachability from {ancestor} to {descendant}; fetch full history."
    )


def _path_is_dirty(repo_root: Path, path: Path) -> bool:
    result = _git(
        repo_root,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--",
        path.as_posix(),
    )
    if result.returncode != 0:
        raise ActionPinError(f"Could not inspect worktree state for {path}.")
    return bool(result.stdout.strip())
