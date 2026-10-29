"""Account for shared addon SHA ranges without waiving unexplained input changes."""

import click

from control_plane.artifact_release_inputs import (
    verified_release_inputs,
    repository_key,
)
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.release_review import SharedSourceReview
from control_plane.release_review_github import GitHubRead, read_release_changes


def _selectors(artifact: ArtifactIdentityManifest, repository: str) -> dict[str, str]:
    sources = {repository_key(source.repository): source.ref for source in artifact.addon_sources}
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
    review_label: str = "owner-review",
    policy_store: object = None,
) -> tuple[tuple[SharedSourceReview, ...], tuple[str, ...]]:
    before_inputs, before_unknown = verified_release_inputs(production)
    after_inputs, after_unknown = verified_release_inputs(candidate)
    unexplained = list(dict.fromkeys((*before_unknown, *after_unknown)))
    ranges = set()
    for name in sorted(before_inputs.keys() | after_inputs.keys()):
        before = before_inputs.get(name)
        after = after_inputs.get(name)
        if before is None or after is None:
            unexplained.append(
                f"{name}: UNKNOWN input was added or removed without review coverage."
            )
            continue
        if not before.has_source_identity or not after.has_source_identity:
            continue
        if before.repository != after.repository:
            unexplained.append(
                f"{name}: UNKNOWN source repository changed without review coverage."
            )
            continue
        if before.repository == repository_key(repository):
            if before.commit != production.source_commit or after.commit != candidate.source_commit:
                unexplained.append(f"{name}: UNKNOWN source differs from the product commit.")
            continue
        if before.commit != after.commit:
            ranges.add((before.repository, before.commit, after.commit))
    try:
        before_selectors = _selectors(production, repository)
        after_selectors = _selectors(candidate, repository)
    except ValueError:
        before_selectors, after_selectors = {}, {}
        unexplained.append("Shared release input source selection is UNKNOWN or ambiguous.")
    reviews = []
    for key in sorted(before_selectors.keys() | after_selectors.keys()):
        if before_selectors.get(key) != after_selectors.get(key):
            unexplained.append(
                f"Shared release input {key} changed its source selection without review coverage."
            )
    for key, production_commit, candidate_commit in sorted(ranges):
        try:
            items, untracked = read_release_changes(
                repository=key,
                production_commit=production_commit,
                candidate_commit=candidate_commit,
                read=read,
                preview_hosts=preview_hosts,
                review_label=review_label,
                policy_store=policy_store,
            )
        except (ValueError, click.ClickException):
            # Preserve the existing release-scoped admin review path. Client
            # acceptance still cannot waive unavailable shared coverage.
            unexplained.append(
                f"Shared release input {key} could not be fully reviewed "
                f"({production_commit} to {candidate_commit}). Admin review is required."
            )
            continue
        reviews.append(
            SharedSourceReview(
                repository=key,
                production_commit=production_commit,
                candidate_commit=candidate_commit,
                items=items,
                untracked_commits=untracked,
            )
        )
    return tuple(reviews), tuple(dict.fromkeys(unexplained))
