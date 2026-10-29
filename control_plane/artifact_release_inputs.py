import re
from collections.abc import Iterator
from typing import NamedTuple
from urllib.parse import urlsplit

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest


def repository_key(value: str) -> str:
    if value.startswith("git@github.com:"):
        value = value.removeprefix("git@github.com:")
    elif "://" in value:
        parsed = urlsplit(value)
        if parsed.hostname == "github.com":
            value = str(parsed.path).strip("/")
    return value.removesuffix(".git").casefold()


class ReleaseInput(NamedTuple):
    name: str
    repository: str
    commit: str

    @property
    def has_source_identity(self) -> bool:
        return bool(
            re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", self.repository)
            and re.fullmatch(r"[0-9a-f]{40}", self.commit)
        )


def recorded_release_inputs(artifact: ArtifactIdentityManifest) -> Iterator[ReleaseInput]:
    for source in artifact.addon_sources:
        key = repository_key(source.repository)
        yield ReleaseInput(f"Addon {key}", key, source.ref)
    for image in artifact.build_provenance.base_images:
        yield ReleaseInput(
            f"Base image {image.role}", repository_key(image.source_repository), image.source_ref
        )
    for tool in artifact.build_provenance.build_tools:
        yield ReleaseInput(
            f"Build tool {tool.name.casefold()}",
            repository_key(tool.source_repository),
            tool.source_ref,
        )


def recorded_release_repositories(artifact: ArtifactIdentityManifest) -> set[str]:
    return {
        source.repository for source in recorded_release_inputs(artifact) if source.repository
    } | {repository_key(selector.repository) for selector in artifact.addon_selectors}


def verified_release_inputs(
    artifact: ArtifactIdentityManifest,
) -> tuple[dict[str, ReleaseInput], tuple[str, ...]]:
    inputs = {}
    unknown = []
    for source in recorded_release_inputs(artifact):
        if source.name in inputs:
            unknown.append(f"{source.name}: UNKNOWN source identity is ambiguous.")
        if not source.has_source_identity:
            unknown.append(f"{source.name}: UNKNOWN verified source repository and commit.")
        inputs[source.name] = source
    roles = {image.role for image in artifact.build_provenance.base_images}
    for role in ("runtime", "devtools"):
        if role not in roles:
            unknown.append(f"Base image {role}: UNKNOWN source provenance is missing.")
    if not artifact.build_provenance.build_tools:
        unknown.append("Build tools: UNKNOWN source provenance is missing.")
    return inputs, tuple(unknown)
