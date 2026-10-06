"""Keep repository-owned changes on their original pull request.

This is conservative batch routing, not an implementation of GitHub approval
rules. GitHub remains responsible for deciding whether an owner approved.
"""

import base64
import binascii
from fnmatch import fnmatchcase
from urllib.parse import quote
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from control_plane.merge_train import MergeTrainPullRequestSnapshot
    from control_plane.merge_train_github import MergeTrainGitHubTransport


_CODEOWNERS_PATHS = (".github/CODEOWNERS", "CODEOWNERS", "docs/CODEOWNERS")


def individual_landing_snapshots(
    *,
    transport: "MergeTrainGitHubTransport",
    repository_path: str,
    base_sha: str,
    pull_requests: tuple["MergeTrainPullRequestSnapshot", ...],
) -> tuple["MergeTrainPullRequestSnapshot", ...]:
    from control_plane.merge_train_github import MergeTrainGitHubError

    if not any(pr.state == "open" and not pr.is_draft and pr.labels for pr in pull_requests):
        return pull_requests
    if not base_sha:
        raise MergeTrainGitHubError("Code-owner batch routing requires the current base SHA.")
    patterns = _read_patterns(transport, repository_path, base_sha)
    if not patterns:
        return pull_requests
    result = []
    for pr in pull_requests:
        if pr.state != "open" or pr.is_draft or not pr.labels:
            result.append(pr)
            continue
        try:
            paths = _changed_paths(transport, repository_path, pr.number)
            confirmation = transport.request(
                method="GET", path=f"/repos/{repository_path}/pulls/{pr.number}"
            )
            if (
                not isinstance(confirmation, dict)
                or not isinstance(confirmation.get("head"), dict)
                or confirmation["head"].get("sha") != pr.head_sha
            ):
                raise MergeTrainGitHubError("Code-owner routing head changed during file reads.")
            individual = any(
                path in _CODEOWNERS_PATHS or any(_may_match(path, pattern) for pattern in patterns)
                for path in paths
            )
        except MergeTrainGitHubError as error:
            if error.rate_limited:
                raise
            # Incomplete evidence can never justify transferring this PR's
            # approval to a batch. Other PRs may still proceed.
            individual = True
        result.append(pr.model_copy(update={"requires_individual_landing": individual}))
    return tuple(result)


def _changed_paths(
    transport: "MergeTrainGitHubTransport", repository_path: str, number: int
) -> tuple[str, ...]:
    from control_plane.merge_train_github import MergeTrainGitHubError

    paths: list[str] = []
    for page in range(1, 31):
        files = transport.request(
            method="GET",
            path=f"/repos/{repository_path}/pulls/{number}/files?per_page=100&page={page}",
        )
        if not isinstance(files, list):
            raise MergeTrainGitHubError("Code-owner routing requires complete changed files.")
        for file in files:
            if (
                not isinstance(file, dict)
                or not isinstance(file.get("filename"), str)
                or not file["filename"]
            ):
                raise MergeTrainGitHubError("Code-owner routing received a malformed file.")
            paths.append(file["filename"])
            if file.get("status") == "renamed":
                previous = file.get("previous_filename")
                if not isinstance(previous, str) or not previous:
                    raise MergeTrainGitHubError("Code-owner routing requires rename origins.")
                paths.append(previous)
        if len(files) < 100:
            if not paths:
                raise MergeTrainGitHubError("Code-owner routing requires nonempty changed files.")
            return tuple(paths)
    raise MergeTrainGitHubError("Code-owner routing exceeded the changed-file page bound.")


def _read_patterns(
    transport: "MergeTrainGitHubTransport", repository_path: str, base_sha: str
) -> tuple[str, ...]:
    from control_plane.merge_train_github import MergeTrainGitHubError

    for path in _CODEOWNERS_PATHS:
        try:
            payload = transport.request(
                method="GET",
                path=f"/repos/{repository_path}/contents/{path}?ref={quote(base_sha, safe='')}",
            )
        except MergeTrainGitHubError as error:
            if error.status_code == 404:
                continue
            raise
        if (
            not isinstance(payload, dict)
            or payload.get("encoding") != "base64"
            or not isinstance(payload.get("content"), str)
        ):
            raise MergeTrainGitHubError("Code-owner routing requires readable CODEOWNERS content.")
        try:
            content = base64.b64decode("".join(payload["content"].split()), validate=True).decode(
                "utf-8"
            )
        except (ValueError, binascii.Error, UnicodeDecodeError) as error:
            raise MergeTrainGitHubError(
                "Code-owner routing received invalid CODEOWNERS content."
            ) from error
        # Keep every owned pattern, including ones later overridden. This may
        # route an unowned change individually; it cannot drop an owner rule.
        return tuple(
            line.split()[0]
            for raw_line in content.splitlines()
            if (line := raw_line.split("#", 1)[0].strip()) and len(line.split()) > 1
        )
    return ()


def _may_match(path: str, pattern: str) -> bool:
    # Unusual syntax is routed individually instead of guessed. Routing can
    # overmatch, but must never erase the approval attached to an owned PR.
    if "**" in pattern or any(character in pattern for character in "\\[]!"):
        return True
    anchored = pattern.startswith("/")
    pattern = pattern.lstrip("/").rstrip("/")
    prefixes = tuple(
        "/".join(path.split("/")[:index]) for index in range(1, len(path.split("/")) + 1)
    )
    if anchored or "/" in pattern:
        return any(fnmatchcase(prefix, pattern) for prefix in prefixes)
    return any(fnmatchcase(component, pattern) for component in path.split("/"))
