"""Compile and evaluate release review from current Launchplane lane records."""

import hashlib
import json
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import urlsplit

import click

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_review import ProductReviewDecisionRecord
from control_plane.contracts.release_review import (
    ReleaseChecklist,
    ReleaseReviewDecisionRecord,
    ReleaseReviewStatus,
    ReleaseVersion,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.release_review_github import GitHubRead, read_release_changes
from control_plane.workflows.launchplane import github_api_request, resolve_launchplane_github_token


class ReleaseReviewStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_release_tuple_record(
        self, *, context_name: str, channel_name: str
    ) -> ReleaseTupleRecord: ...

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest: ...

    def read_environment_inventory(
        self, *, context_name: str, instance_name: str
    ) -> EnvironmentInventory: ...

    def list_product_review_decision_records(
        self, *, repository: str, pull_request_number: int, limit: int | None = None
    ) -> tuple[ProductReviewDecisionRecord, ...]: ...

    def write_release_review_decision_record(
        self, record: ReleaseReviewDecisionRecord
    ) -> object: ...

    def list_release_review_decision_records(
        self, *, product: str, limit: int | None = None
    ) -> tuple[ReleaseReviewDecisionRecord, ...]: ...


RELEASE_RECORD_PENDING = (
    "The decision is saved, but the release record could not be published. Try recording it again."
)


def checklist_digest(checklist: ReleaseChecklist) -> str:
    payload = checklist.model_dump(mode="json")
    # Prior preview decisions are helpful annotations, never release approval.
    for item in payload["items"]:
        item.pop("already_reviewed")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def release_version(
    *, store: ReleaseReviewStore, profile: LaunchplaneProductProfileRecord, instance: str
) -> ReleaseVersion:
    lane = next((lane for lane in profile.lanes if lane.instance == instance), None)
    if lane is None:
        raise ValueError(f"Product has no {instance} lane.")
    if profile.driver_id == "odoo":
        release = store.read_release_tuple_record(context_name=lane.context, channel_name=instance)
        if release.context != lane.context or release.channel != instance:
            raise ValueError("Release tuple does not belong to the requested lane.")
        artifact = store.read_artifact_manifest(release.artifact_id)

        def repository_key(value: str) -> str:
            if value.startswith("git@github.com:"):
                value = value.removeprefix("git@github.com:")
            elif "://" in value:
                parsed = urlsplit(value)
                if parsed.hostname == "github.com":
                    value = parsed.path.strip("/")
            return value.removesuffix(".git").casefold()

        sources = sorted(
            (repository_key(source.repository), source.ref)
            for source in artifact.addon_sources
            if repository_key(source.repository) != repository_key(profile.repository)
        )
        selectors = sorted(
            (repository_key(selector.repository), selector.selector, selector.resolved_ref)
            for selector in artifact.addon_selectors
            if repository_key(selector.repository) != repository_key(profile.repository)
        )
        shared_digest = hashlib.sha256(json.dumps([sources, selectors]).encode()).hexdigest()
        return ReleaseVersion(
            artifact_id=release.artifact_id,
            source_commit=artifact.source_commit,
            shared_addons_digest=shared_digest,
        )
    inventory = store.read_environment_inventory(context_name=lane.context, instance_name=instance)
    if inventory.context != lane.context or inventory.instance != instance:
        raise ValueError("Environment inventory does not belong to the requested lane.")
    identity = inventory.runtime_identity
    if identity is None or inventory.deploy.status != "pass":
        raise ValueError("Release requires a successfully deployed runtime identity.")
    if identity.product != profile.product or identity.instance != instance:
        raise ValueError("Runtime identity does not belong to the requested product lane.")
    return ReleaseVersion(artifact_id=identity.artifact_id, source_commit=identity.source_git_ref)


def build_release_review(
    *, store: ReleaseReviewStore, profile: LaunchplaneProductProfileRecord, read: GitHubRead
) -> ReleaseReviewStatus:
    production = release_version(store=store, profile=profile, instance="prod")
    candidate = release_version(store=store, profile=profile, instance="testing")
    lane = next(lane for lane in profile.lanes if lane.instance == "testing")
    items, untracked = read_release_changes(
        repository=profile.repository,
        production_commit=production.source_commit,
        candidate_commit=candidate.source_commit,
        read=read,
    )
    annotated = []
    for item in items:
        decisions = store.list_product_review_decision_records(
            repository=profile.repository, pull_request_number=item.pull_request_number
        )
        owner_decisions = [
            decision
            for decision in decisions
            if decision.product == profile.product
            and decision.owner_github_id == profile.owner.github_id
        ]
        latest = max(
            owner_decisions,
            key=lambda decision: (decision.decided_at, decision.record_id),
            default=None,
        )
        annotated.append(
            item.model_copy(
                update={
                    "already_reviewed": bool(
                        latest
                        and latest.decision == "accepted"
                        and latest.head_sha == item.head_sha
                    )
                }
            )
        )
    checklist = ReleaseChecklist(
        product=profile.product,
        repository=profile.repository,
        owner_github_id=profile.owner.github_id,
        testing_url=lane.base_url,
        production=production,
        candidate=candidate,
        items=tuple(annotated),
        untracked_commits=untracked,
        additional_changes=(
            (
                "Shared website components changed outside this repository's checklist. Operator review is required.",
            )
            if production.shared_addons_digest != candidate.shared_addons_digest
            else ()
        ),
    )
    digest = checklist_digest(checklist)
    matching = [
        decision
        for decision in store.list_release_review_decision_records(product=profile.product)
        if decision.checklist_digest == digest
    ]
    latest_decision = max(
        matching, key=lambda decision: (decision.decided_at, decision.record_id), default=None
    )
    blockers = checklist_blockers(checklist)
    approved = bool(latest_decision and latest_decision.release_issue_url) and (
        bool(latest_decision and latest_decision.decision == "overridden")
        or bool(not blockers and latest_decision and latest_decision.decision == "accepted")
    )
    if latest_decision and not latest_decision.release_issue_url:
        blockers += (RELEASE_RECORD_PENDING,)
    if not approved and not blockers:
        blockers = (
            "The Owner requested changes."
            if latest_decision
            else "Owner approval of this release is required.",
        )
    return ReleaseReviewStatus(
        required=profile.production_use != "prelaunch",
        approved=approved,
        checklist=checklist,
        checklist_digest=digest,
        blockers=() if approved else blockers,
        latest_decision=latest_decision,
    )


def checklist_blockers(checklist: ReleaseChecklist) -> tuple[str, ...]:
    blockers = []
    if not checklist.owner_github_id:
        blockers.append("No Owner set for this product.")
    if not checklist.testing_url:
        blockers.append("The testing site URL is unavailable.")
    for item in checklist.items:
        if not item.owner_test_notes:
            blockers.append(f"Pull request #{item.pull_request_number} has no Owner test notes.")
    if checklist.untracked_commits:
        blockers.append(
            "The release contains commits without a merged pull request and Owner test notes."
        )
    blockers.extend(checklist.additional_changes)
    return tuple(blockers)


def current_release_review(
    *,
    control_plane_root: Path,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    include_prelaunch: bool = False,
) -> ReleaseReviewStatus:
    if profile.production_use == "prelaunch" and not include_prelaunch:
        return ReleaseReviewStatus(required=False, approved=True)
    try:
        lane = next(lane for lane in profile.lanes if lane.instance == "testing")
        token = resolve_launchplane_github_token(
            control_plane_root=control_plane_root, context_name=lane.context
        )
        if not token:
            raise ValueError("Release checklist source-control access is unavailable.")
        return build_release_review(
            store=cast(ReleaseReviewStore, record_store),
            profile=profile,
            read=lambda path: github_api_request(path=path, token=token),
        )
    except (AttributeError, FileNotFoundError, StopIteration, ValueError, click.ClickException):
        # Provider errors may contain private URLs. Keep refusal evidence bounded.
        return ReleaseReviewStatus(
            blockers=(
                "The current release checklist is unavailable. Verify testing, production and source-control evidence.",
            )
        )


def require_release_approval(
    *,
    control_plane_root: Path,
    record_store: object,
    product: str,
    artifact_id: str = "",
    source_commit: str = "",
) -> None:
    profile = cast(ReleaseReviewStore, record_store).read_product_profile_record(product)
    review = current_release_review(
        control_plane_root=control_plane_root, record_store=record_store, profile=profile
    )
    if not review.required:
        return
    if not review.approved or review.checklist is None:
        raise click.ClickException(
            " ".join(review.blockers) or "Owner release approval is required."
        )
    candidate = review.checklist.candidate
    if (artifact_id and artifact_id != candidate.artifact_id) or (
        source_commit and source_commit != candidate.source_commit
    ):
        raise click.ClickException("Promotion no longer matches the approved testing release.")
