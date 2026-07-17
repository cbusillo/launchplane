from __future__ import annotations

from collections.abc import Sequence
import hashlib
import json
from pathlib import PurePosixPath
import re
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ArtifactUvLockScope = Literal["support_runtime", "tenant"]
ArtifactPythonPackageSourceKind = Literal["registry", "vcs"]
ArtifactExternalDependencyFormat = Literal["pyproject_toml", "requirements_txt"]
ArtifactExternalResolutionPosture = Literal["locked", "exact_source_unlocked"]

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_SHA256_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
_GIT_COMMIT_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REPOSITORY_IDENTITY_PATTERN = re.compile(r"^[\x21-\x7e]+$")
_REPOSITORY_URL_PATH_PATTERN = re.compile(r"^/[A-Za-z0-9._/-]+$")
_SCP_REPOSITORY_PATTERN = re.compile(
    r"^(?P<username>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9.-]+):"
    r"(?P<path>[A-Za-z0-9._/-]+)$"
)
_SSH_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
_CANONICAL_PACKAGE_NAME_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")
_EXACT_PACKAGE_VERSION_PATTERN = re.compile(r"^(?=.*[0-9])[A-Za-z0-9][A-Za-z0-9.!+_-]*$")
_REPO_RELATIVE_PATH_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
_OCI_PLATFORM_PATTERN = re.compile(
    r"^[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)?$"
)
_PYTHON_VERSION_PATTERN = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[a-z0-9.+-]+)?$")


def canonical_python_package_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value.strip()).lower()


def normalize_artifact_sha256(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not _SHA256_PATTERN.fullmatch(normalized):
        raise ValueError(f"{label} must be a lowercase 64-character SHA-256 value")
    return normalized


def normalize_artifact_sha256_digest(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not _SHA256_DIGEST_PATTERN.fullmatch(normalized):
        raise ValueError(f"{label} must be a lowercase sha256 digest")
    return normalized


def normalize_artifact_git_commit(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not _GIT_COMMIT_PATTERN.fullmatch(normalized):
        raise ValueError(f"{label} must be an exact lowercase 40-character git commit")
    return normalized


def normalize_artifact_repository_identity(value: str, *, label: str) -> str:
    normalized = value.strip()
    lowered = normalized.lower()
    if not normalized:
        raise ValueError(f"{label} requires repository identity")
    if not _REPOSITORY_IDENTITY_PATTERN.fullmatch(normalized):
        raise ValueError(f"{label} contains unsafe repository identity characters")
    if (
        normalized.startswith(("/", "./", "../", "~"))
        or _WINDOWS_DRIVE_PATTERN.match(normalized)
        or "\\" in normalized
        or lowered.startswith(("file:", "git+file:"))
    ):
        raise ValueError(f"{label} cannot use a local repository path")

    scp_match = _SCP_REPOSITORY_PATTERN.fullmatch(normalized)
    if scp_match is not None:
        path = scp_match.group("path")
        _validate_repository_path(f"/{path}", label=label)
        return (
            f"ssh://{scp_match.group('username')}@{scp_match.group('host').lower()}"
            f"/{path.rstrip('/')}"
        )

    if "://" not in normalized:
        if not _REPOSITORY_SLUG_PATTERN.fullmatch(normalized):
            raise ValueError(f"{label} must use an owner/repository slug or sanitized URL")
        if any(part in {".", ".."} for part in normalized.split("/")):
            raise ValueError(f"{label} cannot contain repository path traversal")
        return normalized

    parsed = urlsplit(normalized)
    if parsed.scheme not in {"https", "ssh"}:
        raise ValueError(f"{label} must use an https or ssh repository URL")
    if parsed.password is not None:
        raise ValueError(f"{label} cannot contain URL userinfo")
    username = parsed.username
    if username is not None and (
        parsed.scheme != "ssh" or not _SSH_USERNAME_PATTERN.fullmatch(username)
    ):
        raise ValueError(f"{label} cannot contain URL userinfo")
    if parsed.query:
        raise ValueError(f"{label} cannot contain a query string")
    if parsed.fragment:
        raise ValueError(f"{label} cannot contain a URL fragment")
    if not parsed.hostname or not parsed.path.strip("/"):
        raise ValueError(f"{label} requires a repository host and path")
    _validate_repository_path(parsed.path, label=label)

    host = parsed.hostname.lower()
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"{label} contains an invalid repository port") from error
    authority = f"{host}:{port}" if port is not None else host
    netloc = f"{username}@{authority}" if username is not None else authority
    return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))


def _validate_repository_path(value: str, *, label: str) -> None:
    if not _REPOSITORY_URL_PATH_PATTERN.fullmatch(value):
        raise ValueError(f"{label} contains unsafe repository path characters")
    parts = value.strip("/").split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError(f"{label} cannot contain repository path traversal")


def normalize_artifact_relative_path(value: str, *, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} requires a repo-relative path")
    if (
        normalized.startswith(("/", "~"))
        or _WINDOWS_DRIVE_PATTERN.match(normalized)
        or "\\" in normalized
        or "://" in normalized
    ):
        raise ValueError(f"{label} must be a repo-relative path")
    if not _REPO_RELATIVE_PATH_PATTERN.fullmatch(normalized):
        raise ValueError(f"{label} contains unsafe path characters")
    path = PurePosixPath(normalized)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"{label} must be a repo-relative path without traversal")
    if path.as_posix() != normalized:
        raise ValueError(f"{label} must use canonical POSIX path syntax")
    return normalized


def normalize_artifact_platform(value: str, *, label: str) -> str:
    normalized = value.strip().lower()
    if not _OCI_PLATFORM_PATTERN.fullmatch(normalized):
        raise ValueError(f"{label} contains an invalid OCI platform")
    return normalized


class ArtifactUvLockProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: ArtifactUvLockScope
    source_repository: str
    source_ref: str
    path: str
    sha256: str

    @model_validator(mode="after")
    def _validate_lock(self) -> "ArtifactUvLockProvenance":
        self.source_repository = normalize_artifact_repository_identity(
            self.source_repository,
            label="artifact uv lock provenance",
        )
        self.source_ref = normalize_artifact_git_commit(
            self.source_ref,
            label="artifact uv lock source_ref",
        )
        self.path = normalize_artifact_relative_path(
            self.path,
            label="artifact uv lock path",
        )
        if PurePosixPath(self.path).name != "uv.lock":
            raise ValueError("artifact uv lock path must identify uv.lock")
        self.sha256 = normalize_artifact_sha256(
            self.sha256,
            label="artifact uv lock sha256",
        )
        return self


class ArtifactPythonPackageSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ArtifactPythonPackageSourceKind
    repository: str = ""
    commit: str = ""

    @model_validator(mode="after")
    def _validate_source(self) -> "ArtifactPythonPackageSource":
        self.repository = self.repository.strip()
        self.commit = self.commit.strip()
        if self.kind == "registry":
            if self.repository or self.commit:
                raise ValueError(
                    "registry package source cannot contain repository or commit evidence"
                )
            return self
        self.repository = normalize_artifact_repository_identity(
            self.repository,
            label="artifact python package vcs source",
        )
        self.commit = normalize_artifact_git_commit(
            self.commit,
            label="artifact python package vcs commit",
        )
        return self


class ArtifactPythonPackage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: str
    source: ArtifactPythonPackageSource

    @model_validator(mode="after")
    def _validate_package(self) -> "ArtifactPythonPackage":
        normalized_name = canonical_python_package_name(self.name)
        if (
            not normalized_name
            or normalized_name != self.name
            or not _CANONICAL_PACKAGE_NAME_PATTERN.fullmatch(normalized_name)
        ):
            raise ValueError("artifact python package name must use canonical package syntax")
        self.version = self.version.strip()
        if not _EXACT_PACKAGE_VERSION_PATTERN.fullmatch(self.version):
            raise ValueError("artifact python package requires an exact installed version")
        return self


def artifact_python_package_inventory_sha256(
    packages: Sequence[ArtifactPythonPackage],
) -> str:
    payload = [
        package.model_dump(mode="json") for package in sorted(packages, key=lambda item: item.name)
    ]
    canonical_json = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class ArtifactPythonEnvironmentProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    python_version: str
    packages: tuple[ArtifactPythonPackage, ...]
    package_count: int = Field(ge=0)
    packages_sha256: str

    @field_validator("python_version", mode="after")
    @classmethod
    def _validate_python_version(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not _PYTHON_VERSION_PATTERN.fullmatch(normalized):
            raise ValueError("artifact python environment requires an exact Python version")
        return normalized

    @model_validator(mode="after")
    def _validate_environment(self) -> "ArtifactPythonEnvironmentProvenance":
        packages = tuple(sorted(self.packages, key=lambda package: package.name))
        names = [package.name for package in packages]
        if len(names) != len(set(names)):
            raise ValueError("artifact python environment cannot contain duplicate packages")
        self.packages = packages
        if self.package_count != len(packages):
            raise ValueError("artifact python environment package_count does not match packages")
        self.packages_sha256 = normalize_artifact_sha256(
            self.packages_sha256,
            label="artifact python environment packages_sha256",
        )
        expected_digest = artifact_python_package_inventory_sha256(packages)
        if self.packages_sha256 != expected_digest:
            raise ValueError("artifact python environment package inventory digest does not match")
        return self


class ArtifactExternalCompatibilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_repository: str
    source_ref: str
    dependency_file_path: str
    dependency_file_sha256: str
    format: ArtifactExternalDependencyFormat
    resolution_posture: ArtifactExternalResolutionPosture

    @model_validator(mode="after")
    def _validate_input(self) -> "ArtifactExternalCompatibilityInput":
        self.source_repository = normalize_artifact_repository_identity(
            self.source_repository,
            label="artifact external compatibility input",
        )
        self.source_ref = normalize_artifact_git_commit(
            self.source_ref,
            label="artifact external compatibility source_ref",
        )
        self.dependency_file_path = normalize_artifact_relative_path(
            self.dependency_file_path,
            label="artifact external compatibility dependency_file_path",
        )
        dependency_file_name = PurePosixPath(self.dependency_file_path).name
        if self.format == "pyproject_toml" and dependency_file_name != "pyproject.toml":
            raise ValueError("pyproject_toml compatibility input must identify pyproject.toml")
        if self.format == "requirements_txt" and not dependency_file_name.endswith(".txt"):
            raise ValueError("requirements_txt compatibility input must identify a .txt file")
        self.dependency_file_sha256 = normalize_artifact_sha256(
            self.dependency_file_sha256,
            label="artifact external compatibility dependency_file_sha256",
        )
        return self


class ArtifactDependencyProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_platforms: tuple[str, ...]
    uv_locks: tuple[ArtifactUvLockProvenance, ...]
    python_environments: dict[str, ArtifactPythonEnvironmentProvenance]
    external_compatibility_inputs: tuple[ArtifactExternalCompatibilityInput, ...] = ()

    @model_validator(mode="after")
    def _validate_provenance(self) -> "ArtifactDependencyProvenance":
        normalized_platforms = tuple(
            normalize_artifact_platform(
                platform,
                label="artifact dependency provenance target_platforms",
            )
            for platform in self.target_platforms
        )
        if not normalized_platforms:
            raise ValueError("artifact dependency provenance requires target platforms")
        if len(normalized_platforms) != len(set(normalized_platforms)):
            raise ValueError(
                "artifact dependency provenance cannot contain duplicate target platforms"
            )
        self.target_platforms = tuple(sorted(normalized_platforms))

        scopes = [lock.scope for lock in self.uv_locks]
        if len(scopes) != len(set(scopes)):
            raise ValueError(
                "artifact dependency provenance cannot contain duplicate uv lock scopes"
            )
        required_scopes: set[ArtifactUvLockScope] = {"support_runtime", "tenant"}
        if set(scopes) != required_scopes:
            raise ValueError(
                "artifact dependency provenance requires support_runtime and tenant uv locks"
            )
        scope_order = {"support_runtime": 0, "tenant": 1}
        self.uv_locks = tuple(sorted(self.uv_locks, key=lambda lock: scope_order[lock.scope]))

        normalized_environments: dict[str, ArtifactPythonEnvironmentProvenance] = {}
        for platform, environment in self.python_environments.items():
            normalized_platform = normalize_artifact_platform(
                platform,
                label="artifact dependency provenance python_environments",
            )
            if normalized_platform in normalized_environments:
                raise ValueError(
                    "artifact dependency provenance cannot contain duplicate python environment platforms"
                )
            normalized_environments[normalized_platform] = environment
        if set(normalized_environments) != set(self.target_platforms):
            raise ValueError(
                "artifact dependency provenance requires python environment evidence for every target platform"
            )
        self.python_environments = dict(sorted(normalized_environments.items()))

        inputs = tuple(
            sorted(
                self.external_compatibility_inputs,
                key=lambda item: (
                    item.source_repository,
                    item.source_ref,
                    item.dependency_file_path,
                ),
            )
        )
        identities = [
            (item.source_repository, item.source_ref, item.dependency_file_path) for item in inputs
        ]
        if len(identities) != len(set(identities)):
            raise ValueError(
                "artifact dependency provenance cannot contain duplicate external compatibility inputs"
            )
        self.external_compatibility_inputs = inputs
        return self
