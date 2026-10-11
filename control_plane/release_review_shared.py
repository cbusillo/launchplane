"""Account for shared addon SHA ranges without waiving unexplained input changes."""

import re
from urllib.parse import urlsplit

import click

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.release_review import SharedSourceReview
from control_plane.release_review_github import GitHubRead, read_release_changes


def repository_key(value: str) -> str:
    if value.startswith("git@github.com:"):
        value = value.removeprefix("git@github.com:")
    elif "://" in value:
        parsed = urlsplit(value)
        if parsed.hostname == "github.com":
            value = parsed.path.strip("/")
    return value.removesuffix(".git").casefold()


def _shared_sources(artifact: ArtifactIdentityManifest, repository: str) -> dict[str, str]:
    sources: dict[str, str] = {}
    for source in artifact.addon_sources:
        key = repository_key(source.repository)
        if key == repository_key(repository):
            continue
        if (
            not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", key)
            or not re.fullmatch(r"[0-9a-f]{40}", source.ref)
            or key in sources
        ):
            raise ValueError("Shared source identity is incomplete or ambiguous.")
        sources[key] = source.ref
    return sources


def _selectors(
    artifact: ArtifactIdentityManifest, repository: str, sources: dict[str, str]
) -> dict[str, str]:
    selectors: dict[str, str] = {}
    for selector in artifact.addon_selectors:
        key = repository_key(selector.repository)
        if key == repository_key(repository):
            continue
        if key in selectors or sources.get(key) != selector.resolved_ref:
            raise ValueError("Shared selector does not identify its artifact source.")
        selectors[key] = selector.selector
    return selectors


def read_shared_source_changes(
    *,
    production: ArtifactIdentityManifest,
    candidate: ArtifactIdentityManifest,
    repository: str,
    read: GitHubRead,
    preview_hosts: tuple[str, ...] = (),
) -> tuple[tuple[SharedSourceReview, ...], tuple[str, ...]]:
    try:
        before = _shared_sources(production, repository)
        after = _shared_sources(candidate, repository)
        before_selectors = _selectors(production, repository, before)
        after_selectors = _selectors(candidate, repository, after)
    except ValueError:
        return (), ("Shared website component source evidence is incomplete or ambiguous.",)

    reviews = []
    unexplained = []
    for key in sorted(before.keys() | after.keys()):
        if key not in before or key not in after:
            unexplained.append(
                f"Shared website component {key} was added or removed without review coverage."
            )
            continue
        if before_selectors.get(key) != after_selectors.get(key):
            unexplained.append(
                f"Shared website component {key} changed its source selection without review coverage."
            )
        if before[key] == after[key]:
            continue
        try:
            items, untracked = read_release_changes(
                repository=key,
                production_commit=before[key],
                candidate_commit=after[key],
                read=read,
                preview_hosts=preview_hosts,
            )
        except (ValueError, click.ClickException):
            # Preserve the existing release-scoped admin review path. Client
            # acceptance still cannot waive unavailable shared coverage.
            unexplained.append(
                f"Shared website component {key} could not be fully reviewed "
                f"({before[key]} to {after[key]}). Admin review is required."
            )
            continue
        reviews.append(
            SharedSourceReview(
                repository=key,
                production_commit=before[key],
                candidate_commit=after[key],
                items=items,
                untracked_commits=untracked,
            )
        )
    return tuple(reviews), tuple(unexplained)
