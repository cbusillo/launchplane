"""Client-facing invitation changes from persisted preview and train evidence."""

import hashlib
import re
from urllib.parse import parse_qs, urlsplit

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import ReleaseChecklist, ReleaseReviewItem
from control_plane.product_review import source_control_pull_request_url
from control_plane.release_review import ReleaseReviewStore


def _release_items(
    store: ReleaseReviewStore, checklist: ReleaseChecklist
) -> list[tuple[str, ReleaseReviewItem]]:
    groups = [(checklist.repository, checklist.items)] + [
        (source.repository, source.items) for source in checklist.shared_sources
    ]
    result = []
    for repository, items in groups:
        plans = store.list_merge_train_batch_landing_plan_records(repository=repository)
        for item in items:
            batch = next(
                (
                    record.landing_plan
                    for record in plans
                    if record.landing_plan.candidate_pull_request_number == item.pull_request_number
                    and all(
                        entry.status == "merged" and entry.merge_commit_sha == item.merge_commit
                        for entry in record.landing_plan.entries
                    )
                ),
                None,
            )
            if batch is None:
                result.append((repository, item))
                continue
            # The train's batch notes have one generated ### #number section per
            # constituent. Classification still comes from that PR's records.
            sections = re.split(r"(?m)^### #([0-9]+)[^\n]*\n", item.owner_test_notes)
            notes = dict(zip(sections[1::2], sections[2::2]))
            for entry in batch.entries:
                note = notes.get(str(entry.pull_request_number), "").strip()
                if not note:
                    raise ValueError("Release invitation batch Client notes are unavailable.")
                result.append(
                    (
                        repository,
                        item.model_copy(
                            update={
                                "pull_request_number": entry.pull_request_number,
                                "url": source_control_pull_request_url(
                                    repository=repository,
                                    pull_request_number=entry.pull_request_number,
                                ),
                                "head_sha": entry.expected_head_sha,
                                "owner_test_notes": note,
                            }
                        ),
                    )
                )
    return result


def client_invitation_changes(
    store: ReleaseReviewStore,
    profile: LaunchplaneProductProfileRecord,
    checklist: ReleaseChecklist,
) -> dict[str, ReleaseReviewItem]:
    # Historical feedback persists the rendered request, not a separate flag.
    # Its bound per-PR review URL is request evidence; a preview alone is not.
    requested: set[tuple[str, int]] = set()
    for feedback in store.list_preview_pr_feedback_records(context_name=profile.preview.context):
        if feedback.product != profile.product or feedback.status != "ready":
            continue
        for link in re.findall(r"https?://[^\s<>\"')]+", feedback.comment_markdown):
            parsed = urlsplit(link)
            query = parse_qs(parsed.query)
            if (
                parsed.path == "/ui/owner-review"
                and query.get("repository") == [feedback.repository]
                and query.get("pull_request") == [str(feedback.anchor_pr_number)]
            ):
                requested.add((feedback.repository, feedback.anchor_pr_number))
    changes = {}
    for repository, item in _release_items(store, checklist):
        decisions = store.list_product_review_decision_records(
            repository=repository, pull_request_number=item.pull_request_number
        )
        if (repository, item.pull_request_number) in requested or any(
            decision.product == profile.product
            and decision.owner_github_id == profile.owner.github_id
            for decision in decisions
        ):
            key = hashlib.sha256(f"{repository}:{item.pull_request_number}".encode()).hexdigest()
            changes[key] = item
    return changes
