from collections.abc import Callable
from datetime import datetime, timezone
from functools import cache
from dataclasses import dataclass, field
import logging
import re
from typing import Final, cast
from urllib.parse import quote
from uuid import uuid4

from control_plane.client_test_notes import change_client_review, owner_test_notes
from control_plane.contracts.integration_attestation import IntegrationAttestationRecord
from control_plane.contracts.merge_train_branch_refresh_record import (
    MergeTrainBranchRefreshRecord,
)
from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_review import (
    ProductReviewCarry,
    ProductReviewCarryReason,
    ProductReviewCarryCommit,
    ProductReviewDecisionRecord,
    ProductReviewIntegratedChange,
    ProductReviewTreeRestoration,
    PRODUCT_REVIEW_REASON_MAX_LENGTH,
)
from control_plane.dependency_update_review import DEPENDENCY_UPDATE_NOTES, pure_dependency_update
from control_plane.merge_train_branch_refresh import (
    optional_merge_train_branch_refresh_read_store,
)
from control_plane.product_review import ProductReviewStore, applicable_product_review_decisions
from control_plane.source_control_change import change_fingerprint as _change_fingerprint
from control_plane.source_control_change import patch_positions_match_base_update

# A busy train may refresh a pull request more than once before anyone looks.
MAX_REFRESHES: Final = 10

SourceControlRead = Callable[[str], object]

_LOGGER = logging.getLogger(__name__)


@dataclass
class _IntegrationEvidence:
    changes: dict[int, ProductReviewIntegratedChange] = field(default_factory=dict)
    batch_record_ids: set[str] = field(default_factory=set)
    batches: tuple[MergeTrainBatchCandidateRecord, ...] | None = None


def carry_owner_acceptance(
    *,
    store: ProductReviewStore,
    profile: LaunchplaneProductProfileRecord,
    pull_request_number: int,
    head_sha: str,
    base_branch: str,
    read: SourceControlRead,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ProductReviewDecisionRecord | None:
    """Save and return the carried acceptance for `head_sha`, or None when it does not carry."""

    refresh_store = optional_merge_train_branch_refresh_read_store(store)
    if not base_branch.strip():
        return None
    head = head_sha.strip().lower()
    decisions = applicable_product_review_decisions(
        decisions=store.list_product_review_decision_records(
            repository=profile.repository, pull_request_number=pull_request_number
        ),
        profile=profile,
        pull_request_number=pull_request_number,
    )
    if not decisions or any(record.head_sha.strip().lower() == head for record in decisions):
        return None
    accepted = decisions[0]
    if not _carryable(accepted) or accepted.base_branch != base_branch.strip():
        # Accepted on another base (or one never recorded): the change is not the same.
        return None
    accepted_head = accepted.head_sha.strip().lower()
    refreshes = (
        refresh_store.list_merge_train_branch_refresh_records(
            repository=profile.repository, pull_request_number=pull_request_number
        )
        if refresh_store is not None
        else ()
    )
    comparison = _object(
        read(f"/repos/{_path(profile.repository)}/compare/{accepted_head}...{head}")
    )
    reason: ProductReviewCarryReason
    integration: ProductReviewCarry | None = None
    commits: tuple[ProductReviewCarryCommit, ...] = ()
    if comparison.get("status") == "ahead":
        commits = _client_impact_commits(comparison, accepted_head, head)
        base_commit = ""
        if not commits:
            integration = _accepted_base_integration(
                comparison=comparison,
                accepted_head=accepted_head,
                head=head,
                base_branch=base_branch.strip(),
                store=store,
                profile=profile,
                read=read,
                refreshes=refreshes,
                accepted_record_id=accepted.record_id,
                pull_request_number=pull_request_number,
            )
            if integration is None:
                return None
            commits, base_commit = integration.commits, integration.comparison_base_sha
        reason = integration.reason if integration else "client_impact_none"
    elif comparison.get("status") == "diverged":
        branch = quote(base_branch.strip(), safe="")
        base_commit = _text(
            _object(read(f"/repos/{_path(profile.repository)}/branches/{branch}"), "commit").get(
                "sha"
            )
        ).lower()
        if re.fullmatch(r"[0-9a-f]{40}", base_commit) is None:
            return None
        reason = "unchanged_patch_base_update"
    else:
        return None
    if reason == "unchanged_patch_base_update":
        comparison_read = cache(read)
        accepted_change = _change_fingerprint(
            repository=profile.repository,
            base=base_commit,
            head=accepted.head_sha,
            read=comparison_read,
            include_blobs=False,
            ignore_hunk_positions=True,
        )
        if not accepted_change or accepted_change != _change_fingerprint(
            repository=profile.repository,
            base=base_commit,
            head=head,
            read=comparison_read,
            include_blobs=False,
            ignore_hunk_positions=True,
        ):
            return None
        if not patch_positions_match_base_update(
            repository=profile.repository,
            base=base_commit,
            accepted_head=accepted.head_sha,
            head=head,
            read=comparison_read,
        ):
            return None
        accepted_modes = _change_fingerprint(
            repository=profile.repository,
            base=base_commit,
            head=accepted.head_sha,
            read=comparison_read,
            include_blobs=False,
            ignore_hunk_positions=True,
            include_file_modes=True,
        )
        if accepted_modes is None or accepted_modes != _change_fingerprint(
            repository=profile.repository,
            base=base_commit,
            head=head,
            read=comparison_read,
            include_blobs=False,
            ignore_hunk_positions=True,
            include_file_modes=True,
        ):
            return None
    with store.product_review_lock(
        repository=profile.repository, pull_request_number=pull_request_number
    ):
        latest = applicable_product_review_decisions(
            decisions=store.list_product_review_decision_records(
                repository=profile.repository, pull_request_number=pull_request_number
            ),
            profile=profile,
            pull_request_number=pull_request_number,
        )
        if not latest or latest[0].record_id != accepted.record_id:
            # The Client decided again while this was checked; that decision stands.
            return None
        record = ProductReviewDecisionRecord.model_validate(
            {
                **accepted.model_dump(),
                "record_id": (
                    f"product-review-{profile.product}-pr-{pull_request_number}-{uuid4().hex}"
                ),
                "head_sha": head,
                "decided_at": now()
                .astimezone(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "carried_from": integration
                or ProductReviewCarry(
                    record_id=accepted.record_id,
                    head_sha=accepted_head,
                    reason=reason,
                    refresh_record_ids=(),
                    comparison_base_sha=base_commit,
                    commits=commits,
                ),
            }
        )
        store.write_product_review_decision_record(record)
    _LOGGER.info(
        "Carried a Client acceptance to a later change head.",
        extra={
            "repository": profile.repository,
            "pull_request_number": pull_request_number,
            "from_head": accepted.head_sha,
            "to_head": head,
        },
    )
    return record


def _client_impact_commits(
    comparison: dict[str, object], accepted_head: str, head: str
) -> tuple[ProductReviewCarryCommit, ...]:
    commits = _complete_commits(comparison, accepted_head)
    previous = accepted_head
    evidence: list[ProductReviewCarryCommit] = []
    for value in commits:
        commit = _object(value)
        sha = _text(commit.get("sha"))
        parents = commit.get("parents")
        message = _object(commit.get("commit")).get("message")
        if (
            not isinstance(parents, list)
            or len(parents) != 1
            or _object(parents[0]).get("sha") != previous
            or not _has_none_trailer(message)
        ):
            return ()
        evidence.append(ProductReviewCarryCommit(sha=sha, client_impact="none"))
        previous = sha
    return tuple(evidence) if previous == head else ()


def _complete_commits(
    comparison: dict[str, object], accepted_head: str
) -> tuple[dict[str, object], ...]:
    commits = comparison.get("commits")
    if not isinstance(commits, list) or not 0 < len(commits) <= 250:
        return ()
    if (
        any(
            type(comparison.get(key)) is not int or comparison[key] != expected
            for key, expected in (
                ("total_commits", len(commits)),
                ("ahead_by", len(commits)),
                ("behind_by", 0),
            )
        )
        or _object(comparison.get("merge_base_commit")).get("sha") != accepted_head
    ):
        return ()
    values = tuple(_object(value) for value in commits)
    shas = [_text(value.get("sha")) for value in values]
    if len(set(shas)) != len(shas) or any(
        re.fullmatch(r"[0-9a-f]{40}", sha) is None or sha == accepted_head for sha in shas
    ):
        return ()
    return values


def _accepted_base_integration(
    *,
    comparison: dict[str, object],
    accepted_head: str,
    head: str,
    base_branch: str,
    store: ProductReviewStore,
    profile: LaunchplaneProductProfileRecord,
    read: SourceControlRead,
    refreshes: tuple[MergeTrainBranchRefreshRecord, ...],
    accepted_record_id: str,
    pull_request_number: int,
) -> ProductReviewCarry | None:
    read = cache(read)
    commits = {str(value["sha"]): value for value in _complete_commits(comparison, accepted_head)}
    if not commits:
        return None
    prefix = f"/repos/{_path(profile.repository)}"
    base = _text(
        _object(read(f"{prefix}/branches/{quote(base_branch, safe='')}"), "commit").get("sha")
    )
    if re.fullmatch(r"[0-9a-f]{40}", base) is None:
        return None
    side: list[ProductReviewCarryCommit] = []
    visited: set[str] = set()
    refresh_ids: list[str] = []
    restored_trees: list[ProductReviewTreeRestoration] = []
    attestations: list[IntegrationAttestationRecord] = []
    attestation_reader = getattr(store, "list_integration_attestation_records", None)
    saved_attestations = (
        cast(
            tuple[IntegrationAttestationRecord, ...],
            attestation_reader(
                repository=profile.repository, pull_request_number=pull_request_number
            ),
        )
        if callable(attestation_reader)
        else ()
    )
    current = head
    merged = False
    while current != accepted_head:
        commit = commits.get(current)
        if commit is None or current in visited:
            return None
        parents = commit.get("parents")
        if not isinstance(parents, list) or len(parents) not in {1, 2}:
            return None
        restoration = _restored_side_tree(current, commits)
        if restoration is not None:
            restored_trees.append(restoration)
            visited.update(restoration.commit_shas)
            current = restoration.ancestor_sha
            continue
        declared_none = _has_none_trailer(_object(commit.get("commit")).get("message"))
        refresh = next(
            (
                record
                for record in refreshes
                if (
                    record.result_head_sha == current
                    and record.base_branch == base_branch
                    and len(parents) == 2
                    and record.expected_head_sha == _object(parents[0]).get("sha")
                    and record.merged_base_sha == _object(parents[1]).get("sha")
                )
            ),
            None,
        )
        attestation = next(
            (
                record
                for record in saved_attestations
                if record.evidence.product == profile.product
                and record.evidence.repository == profile.repository
                and record.evidence.pull_request_number == pull_request_number
                and record.evidence.base_branch == base_branch
                and record.evidence.owner_github_id == profile.owner.github_id
                and record.evidence.source_decision_id == accepted_record_id
                and record.evidence.merge_sha == current
                and record.evidence.tree_sha
                == _object(_object(commit.get("commit")).get("tree")).get("sha")
                and len(parents) == 2
                and record.evidence.parent_shas
                == tuple(_object(parent).get("sha") for parent in parents)
                and record.evidence.parent_shas[0] == accepted_head
            ),
            None,
        )
        if not declared_none and refresh is None and attestation is None:
            return None
        if len(parents) == 2:
            imported = _text(_object(parents[1]).get("sha"))
            if re.fullmatch(r"[0-9a-f]{40}", imported) is None or not _on_base_branch(
                repository=profile.repository, commit=imported, base_branch=base, read=read
            ):
                return None
            merged = True
        if declared_none:
            side.append(ProductReviewCarryCommit(sha=current, client_impact="none"))
        elif refresh is not None:
            refresh_ids.append(refresh.record_id)
        elif attestation is not None:
            attestations.append(attestation)
        visited.add(current)
        current = _text(_object(parents[0]).get("sha"))
    if not merged:
        return None
    evidence = _IntegrationEvidence()
    for sha in commits.keys() - visited:
        if not _integrated_change_reviewed(
            sha=sha,
            base_branch=base_branch,
            store=store,
            profile=profile,
            read=read,
            evidence=evidence,
        ):
            return None
    integrated_changes = tuple(evidence.changes[number] for number in sorted(evidence.changes))
    batch_record_ids = tuple(sorted(evidence.batch_record_ids))
    for record in attestations:
        attested = _IntegrationEvidence()
        comparison = _object(
            read(f"{prefix}/compare/{accepted_head}...{record.evidence.merge_sha}")
        )
        attested_commits = _complete_commits(comparison, accepted_head)
        if not attested_commits:
            return None
        for commit in attested_commits:
            sha = str(commit["sha"])
            if sha != record.evidence.merge_sha and not _integrated_change_reviewed(
                sha=sha,
                base_branch=base_branch,
                store=store,
                profile=profile,
                read=read,
                evidence=attested,
            ):
                return None
        if {
            entry.pull_request_number: entry.model_dump(exclude={"engineering_basis"})
            for entry in record.evidence.integrated_changes
        } != {
            number: entry.model_dump(exclude={"engineering_basis"})
            for number, entry in attested.changes.items()
        } or set(record.evidence.batch_record_ids) != attested.batch_record_ids:
            return None
    return ProductReviewCarry(
        record_id=accepted_record_id,
        head_sha=accepted_head,
        reason="accepted_base_integration" if side or attestations else "merge_train_base_refresh",
        comparison_base_sha=base,
        commits=tuple(reversed(side)),
        refresh_record_ids=tuple(reversed(refresh_ids)),
        integrated_changes=integrated_changes,
        batch_record_ids=batch_record_ids,
        restored_trees=tuple(reversed(restored_trees)),
        integration_attestation_ids=tuple(record.record_id for record in reversed(attestations)),
    )


def _restored_side_tree(
    head: str, commits: dict[str, dict[str, object]]
) -> ProductReviewTreeRestoration | None:
    tree = _text(_object(_object(commits[head].get("commit")).get("tree")).get("sha"))
    if re.fullmatch(r"[0-9a-f]{40}", tree) is None:
        return None
    current = head
    skipped: list[str] = []
    while current in commits and current not in skipped:
        commit = commits[current]
        if (
            current != head
            and _object(_object(commit.get("commit")).get("tree")).get("sha") == tree
        ):
            return ProductReviewTreeRestoration(
                head_sha=head,
                ancestor_sha=current,
                tree_sha=tree,
                commit_shas=tuple(reversed(skipped)),
            )
        parents = commit.get("parents")
        if not isinstance(parents, list) or len(parents) != 1:
            return None
        skipped.append(current)
        current = _text(_object(parents[0]).get("sha"))
    return None


def _integrated_change_reviewed(
    *,
    sha: str,
    base_branch: str,
    store: ProductReviewStore,
    profile: LaunchplaneProductProfileRecord,
    read: SourceControlRead,
    evidence: _IntegrationEvidence,
) -> bool:
    prefix = f"/repos/{_path(profile.repository)}"
    changes = read(f"{prefix}/commits/{sha}/pulls?per_page=100")
    if not isinstance(changes, list) or not changes or len(changes) >= 100:
        return False
    numbers = [_object(value).get("number") for value in changes]
    if any(type(number) is not int or number <= 0 for number in numbers):
        return False
    for number in sorted(set(cast(list[int], numbers))):
        if _merged_change_reviewed(
            number=number,
            base_branch=base_branch,
            store=store,
            profile=profile,
            read=read,
            evidence=evidence,
        ):
            return True
    return False


def _merged_change_reviewed(
    *,
    number: int,
    base_branch: str,
    store: ProductReviewStore,
    profile: LaunchplaneProductProfileRecord,
    read: SourceControlRead,
    evidence: _IntegrationEvidence,
    expected_head: str = "",
    ancestors: tuple[int, ...] = (),
) -> bool:
    if number in ancestors or len(ancestors) >= MAX_REFRESHES:
        return False
    if number in evidence.changes:
        return not expected_head or evidence.changes[number].head_sha == expected_head
    change = _object(read(f"/repos/{_path(profile.repository)}/pulls/{number}"))
    base = _object(change.get("base"))
    head = _object(change.get("head"))
    if (
        change.get("merged") is not True
        or base.get("ref") != base_branch
        or _object(base.get("repo")).get("full_name") != profile.repository
        or (expected_head and head.get("sha") != expected_head)
    ):
        return False
    if _text(head.get("ref")).startswith("launchplane/train/"):
        reader = getattr(store, "list_merge_train_batch_candidate_records", None)
        if not callable(reader) or _object(head.get("repo")).get("full_name") != profile.repository:
            return False
        if evidence.batches is None:
            evidence.batches = cast(
                tuple[MergeTrainBatchCandidateRecord, ...],
                reader(repository=profile.repository, base_branch=base_branch),
            )
        for record in evidence.batches:
            if not (
                record.candidate.candidate_sha == head.get("sha")
                and record.candidate.candidate_ref.removeprefix("refs/heads/") == head.get("ref")
            ):
                continue
            if all(
                _merged_change_reviewed(
                    number=entry.pull_request_number,
                    expected_head=entry.head_sha,
                    base_branch=base_branch,
                    store=store,
                    profile=profile,
                    read=read,
                    ancestors=(*ancestors, number),
                    evidence=evidence,
                )
                for entry in record.candidate.entries
            ):
                evidence.batch_record_ids.add(record.record_id)
                return True
        return False
    labels = change.get("labels")
    if not isinstance(labels, list) or not isinstance(change.get("body"), str):
        return False
    dependency_only = pure_dependency_update(change, read=read, store=store)
    intent = change_client_review(
        body=str(change["body"]),
        labels=[_text(_object(label).get("name")) for label in labels],
        review_label=profile.owner.review_label,
        pure_dependency_update=dependency_only,
    )
    if intent.error:
        return False
    decisions = applicable_product_review_decisions(
        decisions=store.list_product_review_decision_records(
            repository=profile.repository, pull_request_number=number
        ),
        profile=profile,
        pull_request_number=number,
    )
    if decisions and decisions[0].decision == "changes_requested":
        return False
    accepted = bool(
        decisions
        and _carryable(decisions[0])
        and decisions[0].head_sha == head.get("sha")
        and decisions[0].base_branch == base_branch
    )
    if intent.review_required and not accepted:
        return False
    evidence.changes[number] = ProductReviewIntegratedChange(
        pull_request_number=number,
        head_sha=str(head.get("sha") or ""),
        decision_record_id=decisions[0].record_id if intent.review_required else "",
        engineering_basis=(
            DEPENDENCY_UPDATE_NOTES
            if dependency_only
            else owner_test_notes(str(change["body"]))[:PRODUCT_REVIEW_REASON_MAX_LENGTH]
        )
        if not intent.review_required
        else "",
    )
    return True


def _has_none_trailer(message: object) -> bool:
    if not isinstance(message, str):
        return False
    # Trailers are the final paragraph, not a matching phrase in the body.
    paragraphs = message.rstrip().split("\n\n")
    if len(paragraphs) < 2:
        return False
    lines = paragraphs[-1].splitlines()
    trailers = [re.fullmatch(r"([A-Za-z][A-Za-z0-9-]*):[ \t]*(.+)", line) for line in lines]
    if not trailers or any(trailer is None for trailer in trailers):
        return False
    impacts = [
        trailer.group(2).strip()
        for trailer in trailers
        if trailer is not None and trailer.group(1).lower() == "client-impact"
    ]
    return impacts == ["none"]


def _carryable(decision: ProductReviewDecisionRecord) -> bool:
    return (
        decision.decision == "accepted"
        and bool(decision.head_sha.strip())
        and not (decision.feedback_requested and not decision.feedback_url)
    )


def _on_base_branch(
    *, repository: str, commit: str, base_branch: str, read: SourceControlRead
) -> bool:
    branch = quote(base_branch, safe="")
    comparison = _object(read(f"/repos/{_path(repository)}/compare/{commit}...{branch}"))
    return comparison.get("status") in {"ahead", "identical"}


def _object(value: object, key: str = "") -> dict[str, object]:
    if key:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _path(repository: str) -> str:
    owner, name = repository.strip().split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def record_decision_base(
    *,
    store: ProductReviewStore,
    profile: LaunchplaneProductProfileRecord,
    pull_request_number: int,
    head_sha: str,
    base_branch: str,
) -> None:
    """Keep the base branch on the newest decision for this head the first time it is
    shown on the pull request, so an acceptance carries only on that base."""

    head = head_sha.strip().lower()
    if not base_branch.strip():
        return
    with store.product_review_lock(
        repository=profile.repository, pull_request_number=pull_request_number
    ):
        latest = applicable_product_review_decisions(
            decisions=store.list_product_review_decision_records(
                repository=profile.repository, pull_request_number=pull_request_number
            ),
            profile=profile,
            pull_request_number=pull_request_number,
        )
        if not latest or latest[0].head_sha.strip().lower() != head or latest[0].base_branch:
            return
        store.write_product_review_decision_record(
            latest[0].model_copy(update={"base_branch": base_branch.strip()})
        )
