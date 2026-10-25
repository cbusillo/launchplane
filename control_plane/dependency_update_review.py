"""Provider evidence for engineering-only dependency changes."""

import base64
from collections.abc import Callable
from pathlib import PurePosixPath
import re
import tomllib
from urllib.parse import quote

from control_plane.client_test_notes import NOTHING_TO_TEST
from control_plane.contracts.merge_train_policy import (
    MergeTrainEnqueuePolicy,
    MergeTrainRepositoryPolicy,
)
from control_plane.merge_train_policy_source import (
    MergeTrainPolicyStoreMissingError,
    resolve_merge_train_policy_record,
)

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
    pull_request: dict[str, object],
    *,
    read: Callable[[str], object],
    store: object = None,
    repository_policy: MergeTrainRepositoryPolicy | None = None,
) -> bool:
    user = pull_request.get("user")
    if not isinstance(user, dict) or user.get("type") != "Bot":
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
    policy = dependency_update_policy(
        pull_request, store=store, repository_policy=repository_policy
    )
    if policy is None or policy.dependency_bot(user.get("id")) is None:
        return False
    path = "/repos/" + "/".join(quote(part, safe="") for part in name.split("/"))
    comparison = _safe_read(
        read, f"{path}/compare/{quote(base_sha, safe='')}...{quote(head_sha, safe='')}"
    )
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
            if (
                filename == "workspace.toml"
                and item.get("status") == "modified"
                and _workspace_pin_only(read=read, path=path, base_sha=base_sha, head_sha=head_sha)
            ):
                continue
            file = PurePosixPath(filename)
            if file.name not in _DEPENDENCY_FILES and not (
                file.name.startswith("requirements") and file.suffix == ".txt"
            ):
                return False
    return True


def _safe_read(read: Callable[[str], object], path: str) -> object:
    try:
        return read(path)
    except Exception as error:
        if getattr(error, "rate_limited", False):
            raise
        return None


def _workspace_pin_only(
    *, read: Callable[[str], object], path: str, base_sha: str, head_sha: str
) -> bool:
    documents = []
    for revision in (base_sha, head_sha):
        payload = _safe_read(read, f"{path}/contents/workspace.toml?ref={quote(revision, safe='')}")
        if not isinstance(payload, dict) or payload.get("encoding") != "base64":
            return False
        content = payload.get("content")
        if not isinstance(content, str):
            return False
        try:
            decoded = base64.b64decode("".join(content.split()), validate=True)
            if len(decoded) != payload.get("size"):
                return False
            documents.append(tomllib.loads(decoded.decode()))
        except (ValueError, UnicodeDecodeError):
            return False
    changed = False
    old, new = documents
    for slot in ("devkit", "runtime"):
        old_repos, new_repos = old.get("repos"), new.get("repos")
        if not isinstance(old_repos, dict) or not isinstance(new_repos, dict):
            return False
        old_repo = old_repos.get(slot)
        new_repo = new_repos.get(slot)
        if not isinstance(old_repo, dict) or not isinstance(new_repo, dict):
            continue
        old_ref, new_ref = old_repo.get("ref"), new_repo.get("ref")
        if old_ref == new_ref:
            continue
        if not all(
            isinstance(ref, str) and re.fullmatch(r"[0-9a-f]{40}", ref)
            for ref in (old_ref, new_ref)
        ):
            return False
        changed = True
        old_repo.pop("ref")
        new_repo.pop("ref")
    return changed and old == new


def dependency_update_policy(
    pull_request: dict[str, object],
    *,
    store: object = None,
    repository_policy: MergeTrainRepositoryPolicy | None = None,
) -> MergeTrainEnqueuePolicy | None:
    base = pull_request.get("base")
    if not isinstance(base, dict):
        return None
    repository = base.get("repo")
    if not isinstance(repository, dict):
        return None
    targets: tuple[MergeTrainRepositoryPolicy, ...]
    if repository_policy is not None:
        targets = (repository_policy,)
    else:
        try:
            targets = resolve_merge_train_policy_record(store).policy.policies
        except MergeTrainPolicyStoreMissingError:
            return None
    return next(
        (
            target.enqueue
            for target in targets
            if target.repository.casefold() == str(repository.get("full_name") or "").casefold()
            and target.base_branch == base.get("ref")
        ),
        None,
    )
