"""Verify a product artifact from GitHub's record of the product's build run.

The product repository builds its own image and never calls Launchplane.
Launchplane reads the build run for an exact commit, checks where it ran, and
takes the image digest from that run's uploaded manifest. Image tags and labels
are never evidence. See docs/artifact-provenance.md.
"""

import io
import json
import zipfile
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.artifact_identity import (
    ArtifactIdentityManifest,
    ArtifactSourceBuild,
    BuildPurpose,
)

BUILD_WORKFLOW_PATH = ".github/workflows/build.yml"
MANIFEST_ARTIFACT_PREFIX = "artifact-manifest-"
# A PR author controls a preview build's upload; never read more than this.
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
FIRST_PARENT_PAGE_LIMIT = 3
_GITHUB_API = "https://api.github.com"


class BuildProvenanceError(Exception):
    """The build run does not prove the artifact; nothing may be recorded."""


class BuildProvenanceTransport(Protocol):
    def get_json(self, path: str) -> object: ...

    def get_bytes(self, path: str) -> bytes: ...


class GitHubBuildProvenanceTransport:
    """Read-only GitHub transport for one repository-scoped installation token."""

    def __init__(self, *, token: str, timeout_seconds: float = 30) -> None:
        self._token = token
        self._timeout_seconds = timeout_seconds

    def get_json(self, path: str) -> object:
        return json.loads(self._read(self._api_request(path)) or b"null")

    def get_bytes(self, path: str) -> bytes:
        """Download a redirected archive without sending the token to the storage host."""
        try:
            with build_opener(_NoRedirect).open(
                self._api_request(path), timeout=self._timeout_seconds
            ) as response:
                return _bounded_read(response, path)
        except HTTPError as redirect:
            location = (
                redirect.headers.get("Location") if redirect.code in {301, 302, 307} else None
            )
            if not location:
                raise BuildProvenanceError(
                    f"GitHub read failed for {path}: {redirect}"
                ) from redirect
        except (URLError, OSError) as error:
            raise BuildProvenanceError(f"GitHub read failed for {path}: {error}") from error
        try:
            with urlopen(Request(url=location), timeout=self._timeout_seconds) as response:
                return _bounded_read(response, path)
        except (HTTPError, URLError, OSError) as error:
            raise BuildProvenanceError(f"GitHub read failed for {path}: {error}") from error

    def _api_request(self, path: str) -> Request:
        return Request(
            url=f"{_GITHUB_API}{path}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def _read(self, request: Request) -> bytes:
        try:
            with urlopen(request, timeout=self._timeout_seconds) as response:
                return bytes(response.read())
        except (HTTPError, URLError, OSError) as error:
            raise BuildProvenanceError(
                f"GitHub read failed for {request.full_url}: {error}"
            ) from error


def _bounded_read(response: object, path: str) -> bytes:
    data = bytes(response.read(MAX_MANIFEST_BYTES + 1))  # type: ignore[attr-defined]
    if len(data) > MAX_MANIFEST_BYTES:
        raise BuildProvenanceError(f"The download from {path} is larger than allowed.")
    return data


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class VerifiedBuildArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manifest: ArtifactIdentityManifest


def verify_build_artifact(
    *,
    transport: BuildProvenanceTransport,
    repository: str,
    repository_id: str,
    commit: str,
    purpose: BuildPurpose,
    context: str,
    image_repository: str,
    pull_request_number: int | None = None,
) -> VerifiedBuildArtifact:
    """Return the manifest GitHub's build run proves for this commit, or raise."""
    repository_path = _repository_path(repository)
    commit = commit.strip().lower()
    if not repository_id.strip():
        raise BuildProvenanceError("The product needs its immutable repository id recorded.")
    if purpose == "preview" and pull_request_number is None:
        raise BuildProvenanceError("A preview artifact needs its pull request number.")

    repository_payload = _object(transport.get_json(f"/repos/{repository_path}"), "repository")
    if str(repository_payload.get("id")) != repository_id.strip():
        raise BuildProvenanceError("The repository's id is not the product's recorded id.")
    default_branch = _text(repository_payload.get("default_branch"), "default branch")

    run = _select_build_run(
        transport=transport,
        repository_path=repository_path,
        repository_id=repository_id.strip(),
        commit=commit,
        purpose=purpose,
        default_branch=default_branch,
    )
    if purpose == "release":
        _require_first_parent(
            transport=transport,
            repository_path=repository_path,
            default_branch=default_branch,
            commit=commit,
        )
    else:
        pull_request = _object(
            transport.get_json(f"/repos/{repository_path}/pulls/{pull_request_number}"),
            "pull request",
        )
        head = _object(pull_request.get("head"), "pull request head")
        if str(head.get("sha") or "").lower() != commit:
            raise BuildProvenanceError("The commit is not the pull request's current head.")

    run_id = _int(run.get("id"), "run id")
    run_attempt = _int(run.get("run_attempt") or 1, "run attempt")
    github_artifact_id, manifest_payload = _download_manifest(
        transport=transport,
        repository_path=repository_path,
        run_id=run_id,
        run_attempt=run_attempt,
    )
    manifest = ArtifactIdentityManifest.model_validate(manifest_payload)
    if manifest.schema_version != 2:
        raise BuildProvenanceError("The build manifest must be schema version 2.")
    if manifest.source_commit != commit:
        raise BuildProvenanceError("The build manifest names a different source commit.")
    if manifest.image.repository.rstrip("/") != image_repository.strip().rstrip("/"):
        raise BuildProvenanceError("The build manifest names a different image repository.")
    tenant_locks = [
        lock
        for lock in (
            manifest.dependency_provenance.uv_locks if manifest.dependency_provenance else ()
        )
        if lock.scope == "tenant"
    ]
    if not tenant_locks or tenant_locks[0].source_repository.casefold() != repository.casefold():
        raise BuildProvenanceError("The build manifest's tenant lock is not from this repository.")

    source_build = ArtifactSourceBuild(
        repository=repository,
        repository_id=repository_id.strip(),
        workflow_path=BUILD_WORKFLOW_PATH,
        event="push" if purpose == "release" else "pull_request",
        purpose=purpose,
        pull_request_number=pull_request_number,
        run_id=run_id,
        run_attempt=run_attempt,
        github_artifact_id=github_artifact_id,
        manifest_artifact_id=manifest.artifact_id,
    )
    return VerifiedBuildArtifact(
        manifest=manifest.model_copy(
            update={
                "artifact_id": f"artifact-{context}-run-{run_id}-{run_attempt}",
                "source_build": source_build,
            }
        )
    )


class VerifiedArtifactStore(Protocol):
    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def write_artifact_manifest(self, manifest: ArtifactIdentityManifest) -> object: ...


def record_verified_build_artifact(
    *, record_store: VerifiedArtifactStore, verified: VerifiedBuildArtifact
) -> ArtifactIdentityManifest:
    """Record a verified release artifact once; a differing record under its key is refused.

    Testing deploys and prod promotions read only this store, so a preview
    artifact is never recorded here: it goes straight to its own preview.
    """
    manifest = verified.manifest
    if manifest.source_build is None or manifest.source_build.purpose != "release":
        raise BuildProvenanceError("Only a release build is recorded for testing and prod.")
    try:
        existing = record_store.read_artifact_manifest(manifest.artifact_id)
    except FileNotFoundError:
        record_store.write_artifact_manifest(manifest)
        return manifest
    if existing.model_dump(mode="json") != manifest.model_dump(mode="json"):
        raise BuildProvenanceError(
            f"Artifact {manifest.artifact_id} is already recorded with different content."
        )
    return existing


def _select_build_run(
    *,
    transport: BuildProvenanceTransport,
    repository_path: str,
    repository_id: str,
    commit: str,
    purpose: BuildPurpose,
    default_branch: str,
) -> dict[str, object]:
    event = "push" if purpose == "release" else "pull_request"
    query = urlencode({"head_sha": commit, "event": event, "per_page": "100"})
    payload = _object(
        transport.get_json(f"/repos/{repository_path}/actions/runs?{query}"), "workflow runs"
    )
    runs = [
        run
        for run in (_object(item, "workflow run") for item in _list(payload.get("workflow_runs")))
        if run.get("path") == BUILD_WORKFLOW_PATH
        and str(run.get("head_sha") or "").lower() == commit
        and run.get("event") == event
        and _repository_id(run.get("repository")) == repository_id
        and _repository_id(run.get("head_repository")) == repository_id
        and run.get("status") == "completed"
        and run.get("conclusion") == "success"
        and (purpose != "release" or run.get("head_branch") == default_branch)
    ]
    if not runs:
        raise BuildProvenanceError(
            f"No successful {event} run of {BUILD_WORKFLOW_PATH} built {commit} in this repository."
        )
    return max(
        runs,
        key=lambda run: (
            _int(run.get("id"), "run id"),
            _int(run.get("run_attempt") or 1, "run attempt"),
        ),
    )


def _require_first_parent(
    *,
    transport: BuildProvenanceTransport,
    repository_path: str,
    default_branch: str,
    commit: str,
) -> None:
    """Require the commit to have been the default branch's tip itself.

    Reachability is not enough: a tag named like the branch, pushed at a commit
    from inside a merged pull request, would run that commit's unreviewed
    workflow. Following only first parents from the tip visits merge results
    and direct pushes, never a pull request's own commits.
    """
    parents: dict[str, str] = {}
    cursor = ""
    for page in range(1, FIRST_PARENT_PAGE_LIMIT + 1):
        query = urlencode({"sha": default_branch, "per_page": "100", "page": str(page)})
        commits = _list(transport.get_json(f"/repos/{repository_path}/commits?{query}"))
        for item in commits:
            entry = _object(item, "commit")
            sha = str(entry.get("sha") or "").lower()
            first_parent = next(iter(_list(entry.get("parents"))), None)
            parents[sha] = (
                str(_object(first_parent, "parent").get("sha") or "").lower()
                if first_parent is not None
                else ""
            )
            if not cursor:
                cursor = sha
        while cursor in parents:
            if cursor == commit:
                return
            cursor = parents[cursor]
        if not cursor or len(commits) < 100:
            break
    raise BuildProvenanceError(
        f"{commit} is not on {default_branch}'s first-parent history within the checked range."
    )


def _download_manifest(
    *,
    transport: BuildProvenanceTransport,
    repository_path: str,
    run_id: int,
    run_attempt: int,
) -> tuple[int, dict[str, object]]:
    name = f"{MANIFEST_ARTIFACT_PREFIX}{run_attempt}"
    payload = _object(
        transport.get_json(
            f"/repos/{repository_path}/actions/runs/{run_id}/artifacts?"
            + urlencode({"name": name, "per_page": "100"})
        ),
        "run artifacts",
    )
    artifacts = [
        artifact
        for artifact in (_object(item, "artifact") for item in _list(payload.get("artifacts")))
        if artifact.get("name") == name
    ]
    if len(artifacts) != 1:
        raise BuildProvenanceError(f"Run {run_id} must upload exactly one {name} artifact.")
    artifact = artifacts[0]
    if artifact.get("expired"):
        raise BuildProvenanceError(f"Run {run_id}'s {name} artifact has expired.")
    artifact_id = _int(artifact.get("id"), "artifact id")
    if _int(artifact.get("size_in_bytes") or 0, "artifact size") > MAX_MANIFEST_BYTES:
        raise BuildProvenanceError(f"Run {run_id}'s {name} artifact is larger than allowed.")
    archive = transport.get_bytes(f"/repos/{repository_path}/actions/artifacts/{artifact_id}/zip")
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zip_file:
            members = [info for info in zip_file.infolist() if not info.is_dir()]
            if len(members) != 1:
                raise BuildProvenanceError(f"The {name} artifact must hold exactly one file.")
            with zip_file.open(members[0]) as member:
                manifest_bytes = member.read(MAX_MANIFEST_BYTES + 1)
            if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                raise BuildProvenanceError(f"The {name} manifest is larger than allowed.")
            manifest = json.loads(manifest_bytes)
    except (zipfile.BadZipFile, json.JSONDecodeError, UnicodeDecodeError) as error:
        raise BuildProvenanceError(f"The {name} artifact is not a readable manifest.") from error
    return artifact_id, _object(manifest, "manifest")


def _repository_path(repository: str) -> str:
    owner, separator, name = repository.strip().partition("/")
    if not owner or not separator or not name or "/" in name:
        raise BuildProvenanceError("The product repository must be owner/name.")
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def _repository_id(value: object) -> str:
    return str(value.get("id")) if isinstance(value, dict) else ""


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BuildProvenanceError(f"GitHub returned an unexpected {label}.")
    return value


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BuildProvenanceError(f"GitHub returned no {label}.")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BuildProvenanceError(f"GitHub returned no {label}.")
    return value.strip()
