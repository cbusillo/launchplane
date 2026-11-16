import click
from typing import TYPE_CHECKING, cast

from control_plane.artifact_release_inputs import (
    verified_release_inputs,
    repository_key,
)
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.client_test_notes import client_test_notes
from control_plane.contracts.release_review import (
    ReleaseChecklist,
    ReleaseReviewItem,
    SharedSourceReview,
)
from control_plane.release_review_github import (
    GitHubRead,
    pull_requests_missing_owner_test_notes,
    read_release_changes,
)
from control_plane.repository_evidence import RepositoryEvidenceError, read_github_changed_files

if TYPE_CHECKING:
    from control_plane.release_review import ReleaseReviewStore


def _accepted_input_changes(
    *,
    checklist: ReleaseChecklist,
    read: GitHubRead,
    store: "ReleaseReviewStore",
) -> tuple[ReleaseReviewItem, ...]:
    from control_plane.release_invitation_changes import InvitationEvidenceStore, _release_items

    covered = []
    for repository, change in _release_items(cast(InvitationEvidenceStore, store), checklist):
        if repository != checklist.repository:
            continue
        item = change.item
        if (
            not item.owner_test_notes.strip()
            or item.client_test_notes_error
            or client_test_notes(item.owner_test_notes).error
            or pull_requests_missing_owner_test_notes(item.owner_test_notes)
        ):
            continue
        decisions = store.list_product_review_decision_records(
            repository=repository, pull_request_number=item.pull_request_number
        )
        owner_decisions = [
            decision
            for decision in decisions
            if decision.product == checklist.product
            and decision.owner_github_id == checklist.owner_github_id
        ]
        latest = max(
            owner_decisions,
            key=lambda decision: (decision.decided_at, decision.record_id),
            default=None,
        )
        if not latest or latest.decision != "accepted" or latest.head_sha != item.head_sha:
            continue
        try:
            files = read_github_changed_files(
                request=read,
                repository_path=repository,
                pull_request_number=item.pull_request_number,
                max_file_pages=30,
            )
        except (RepositoryEvidenceError, ValueError, click.ClickException):
            continue
        if any(file.path in {"workspace.toml", "artifact-inputs.toml"} for file in files):
            covered.append(item.model_copy(update={"already_reviewed": True}))
    return tuple(covered)


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
    product_checklist: ReleaseChecklist | None = None,
) -> tuple[tuple[SharedSourceReview, ...], tuple[str, ...]]:
    before_inputs, before_unknown = verified_release_inputs(production)
    after_inputs, after_unknown = verified_release_inputs(candidate)
    unexplained = list(dict.fromkeys((*before_unknown, *after_unknown)))
    transitions = {}
    ranges = set()
    for name in sorted(before_inputs.keys() | after_inputs.keys()):
        before = before_inputs.get(name)
        after = after_inputs.get(name)
        if any(
            source is not None
            and source.repository == repository_key(repository)
            and source.commit != commit
            for source, commit in (
                (before, production.source_commit),
                (after, candidate.source_commit),
            )
        ):
            unexplained.append(f"{name}: UNKNOWN source differs from the product commit.")
            continue
        if before is None or after is None:
            transitions[name] = (
                f"{name}: UNKNOWN input was added or removed without review coverage."
            )
            continue
        if not before.has_source_identity or not after.has_source_identity:
            continue
        if before.repository != after.repository:
            transitions[name] = (
                f"{name}: UNKNOWN source repository changed without review coverage."
            )
            continue
        if before.repository == repository_key(repository):
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
    if transitions:
        coverage: tuple[ReleaseReviewItem, ...] = ()
        if product_checklist is not None and policy_store is not None:
            try:
                coverage = _accepted_input_changes(
                    checklist=product_checklist,
                    read=read,
                    store=cast("ReleaseReviewStore", policy_store),
                )
            except (ValueError, click.ClickException):
                coverage = ()
        if coverage:
            names = ", ".join(transitions)
            reviews.append(
                SharedSourceReview(
                    repository=repository,
                    production_commit=production.source_commit,
                    candidate_commit=candidate.source_commit,
                    items=tuple(
                        item.model_copy(
                            update={"title": f"{names}: input transition; {item.title}"}
                        )
                        for item in coverage
                    ),
                )
            )
        else:
            unexplained.extend(transitions.values())
    for key in sorted(after_selectors):
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
