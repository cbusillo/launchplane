"""Narrow provider source setup; never deploys or accepts credentials."""

from collections.abc import Callable
from pathlib import PurePosixPath
import re
from typing import Literal

from control_plane.dokploy.api import JsonObject


class DokployComposeSourcePartialError(ValueError):
    """A source update was attempted but the provider/record result is incomplete."""


FetchTarget = Callable[[str, str, Literal["compose", "application"], str], JsonObject]
MutateProvider = Callable[[str, str, str, JsonObject], JsonObject]


def validate_compose_source_inputs(branch: str, compose_path: str) -> None:
    if (
        not re.fullmatch(r"[A-Za-z0-9._/-]+", branch)
        or branch.startswith(("-", "/"))
        or branch.endswith(("/", ".", ".lock"))
        or any(part in branch for part in ("..", "@{", "//"))
        or re.search(r"[\s\x00-\x1f\x7f~^:?*\[\\]", branch)
    ):
        raise ValueError("Compose source requires a valid branch name.")
    path = PurePosixPath(compose_path)
    if (
        not re.fullmatch(r"[A-Za-z0-9._/-]+", compose_path)
        or str(path) == "."
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in compose_path
        or any(ord(char) < 32 for char in compose_path)
    ):
        raise ValueError("Compose path must be relative to the product repository.")


def repository_source_url(repository: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Compose source requires a product owner/repository identity.")
    return f"https://github.com/{repository}.git"


def require_empty_compose_source(payload: JsonObject, compose_id: str) -> None:
    if payload.get("composeId") != compose_id or not payload.get("environmentId"):
        raise ValueError("Compose source setup requires exact provider identity and environment.")
    if payload.get("sourceType") not in (None, "", "github", "git", "raw"):
        raise ValueError("Compose target already has a different source type.")
    # A provider's default sourceType/composePath is not a configured source.
    # Reject partial source and credential bindings as well as complete sources.
    for key in (
        "customGitUrl",
        "customGitBranch",
        "customGitSSHKeyId",
        "repository",
        "owner",
        "githubId",
        "gitlabId",
        "gitlabRepository",
        "gitlabOwner",
        "bitbucketId",
        "bitbucketRepository",
        "bitbucketOwner",
        "giteaId",
        "giteaRepository",
        "giteaOwner",
        "composeFile",
        "dockerImage",
        "repositoryId",
        "branch",
        "gitlabBranch",
        "bitbucketBranch",
        "giteaBranch",
        "gitlabProjectId",
        "gitlabPathNamespace",
    ):
        if payload.get(key):
            raise ValueError("Compose target already has a configured or partial source.")


def configure_empty_compose_source(
    *,
    host: str,
    token: str,
    compose_id: str,
    custom_git_url: str,
    branch: str,
    compose_path: str,
    fetch_target_payload: FetchTarget,
    mutate_provider: MutateProvider,
) -> JsonObject:
    validate_compose_source_inputs(branch, compose_path)
    before = fetch_target_payload(host, token, "compose", compose_id)
    require_empty_compose_source(before, compose_id)
    try:
        mutate_provider(
            host,
            token,
            "/api/compose.update",
            {
                "composeId": compose_id,
                "name": before.get("name") or "",
                "environmentId": before["environmentId"],
                "sourceType": "git",
                "customGitUrl": custom_git_url,
                "customGitBranch": branch,
                "composePath": compose_path,
                "autoDeploy": False,
            },
        )
    except Exception as error:
        raise DokployComposeSourcePartialError(
            "Provider source update was attempted; its outcome is uncertain and records are not updated. "
            "Administrator reconciliation is required before retrying."
        ) from error
    try:
        after = fetch_target_payload(host, token, "compose", compose_id)
        expected: JsonObject = {
            "composeId": compose_id,
            "sourceType": "git",
            "customGitUrl": custom_git_url,
            "customGitBranch": branch,
            "composePath": compose_path,
            "autoDeploy": False,
            "environmentId": before["environmentId"],
        }
        if any(after.get(key) != value for key, value in expected.items()):
            raise ValueError("Dokploy compose source read-back did not match the planned source.")
        for key in ("serverId", "name", "appName"):
            if before.get(key) != after.get(key):
                raise ValueError("Dokploy compose binding changed during source setup.")
        return after
    except Exception as error:
        raise DokployComposeSourcePartialError(
            "Provider source applied; read-back failed and tracked records are not updated. "
            "Administrator reconciliation is required; do not retry with a new key."
        ) from error
