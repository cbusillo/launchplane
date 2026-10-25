"""Provider evidence for engineering-only dependency changes."""

from collections.abc import Callable
from pathlib import PurePosixPath
import re
from urllib.parse import quote

from control_plane.client_test_notes import NOTHING_TO_TEST
from control_plane.merge_train_dependency_updates import DEPENDENCY_UPDATE_BOTS

DEPENDENCY_UPDATE_NOTES = f"{NOTHING_TO_TEST}: Automated dependency manifest and lockfile update."
_DEPENDENCY_FILES = frozenset(
    {
        "pyproject.toml",
        "uv.lock",
        "Pipfile",
        "Pipfile.lock",
        "poetry.lock",
        "package.json",
        "package-lock.json",
        "npm-shrinkwrap.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "Gemfile",
        "Gemfile.lock",
        "Cargo.toml",
        "Cargo.lock",
        "go.mod",
        "go.sum",
        "composer.json",
        "composer.lock",
    }
)


def pure_dependency_update(
    pull_request: dict[str, object], *, read: Callable[[str], object]
) -> bool:
    user = pull_request.get("user")
    if (
        not isinstance(user, dict)
        or user.get("login") not in DEPENDENCY_UPDATE_BOTS
        or user.get("type") != "Bot"
    ):
        return False
    head, base = pull_request.get("head"), pull_request.get("base")
    if not isinstance(head, dict) or not isinstance(base, dict):
        return False
    repository = base.get("repo")
    head_repository = head.get("repo")
    if not isinstance(repository, dict) or not isinstance(head_repository, dict):
        return False
    name = repository.get("full_name")
    base_sha, head_sha = base.get("sha"), head.get("sha")
    if (
        not isinstance(name, str)
        or len(name.split("/")) != 2
        or head_repository.get("full_name") != name
        or not isinstance(base_sha, str)
        or not base_sha
        or not isinstance(head_sha, str)
        or not head_sha
    ):
        return False
    path = "/repos/" + "/".join(quote(part, safe="") for part in name.split("/"))
    try:
        comparison = read(f"{path}/compare/{quote(base_sha, safe='')}...{quote(head_sha, safe='')}")
    except Exception as error:
        if getattr(error, "rate_limited", False):
            raise
        return False
    files = comparison.get("files") if isinstance(comparison, dict) else None
    # GitHub comparisons return at most 300 files, even across commit pages.
    if not isinstance(files, list) or not files or len(files) >= 300:
        return False
    changed_files = pull_request.get("changed_files")
    if changed_files is not None and changed_files != len(files):
        return False
    for item in files:
        if not isinstance(item, dict) or item.get("status") not in {
            "added",
            "modified",
            "removed",
            "renamed",
        }:
            return False
        paths = [item.get("filename")]
        if item.get("status") == "renamed":
            paths.append(item.get("previous_filename"))
        for filename in paths:
            if (
                not isinstance(filename, str)
                or ".." in filename.split("/")
                or filename.startswith("/")
            ):
                return False
            if filename == "workspace.toml" and _workspace_pin_only(item):
                continue
            file = PurePosixPath(filename)
            if file.name not in _DEPENDENCY_FILES and not (
                file.name.startswith("requirements") and file.suffix == ".txt"
            ):
                return False
    return True


def _workspace_pin_only(item: dict[str, object]) -> bool:
    patch = item.get("patch")
    if item.get("status") != "modified" or not isinstance(patch, str):
        return False
    changes = [line for line in patch.splitlines() if line.startswith(("+", "-"))]
    additions = sum(line.startswith("+") for line in changes)
    deletions = sum(line.startswith("-") for line in changes)
    return bool(
        additions
        and additions == deletions
        and item.get("additions") == additions
        and item.get("deletions") == deletions
    ) and all(re.fullmatch(r'[+-]\s*ref = "[0-9a-f]{40}"', line) for line in changes)
