"""Compile and evaluate release review from current Launchplane lane records."""

import hashlib
from contextlib import AbstractContextManager, ExitStack
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import unquote

import click

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_review import ProductReviewDecisionRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.release_review import (
    ReleaseChecklist,
    ReleaseEvidenceReason,
    ReleaseReviewDecisionRecord,
    ReleaseReviewStatus,
    ReleaseVersion,
    SharedSourceReview,
)
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.release_review_github import (
    GitHubRead,
    pull_requests_missing_owner_test_notes,
    recorded_preview_hosts,
    read_release_changes,
)
from control_plane.release_review_shared import read_shared_source_changes, repository_key
from control_plane.release_compatibility import classify_release_database_compatibility
from control_plane.workflows.launchplane import (
    github_api_request,
    resolve_launchplane_github_token,
    launchplane_github_token,
)


class ReleaseReviewStore(Protocol):
    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...

    def release_review_publication_lock(
        self, *, record_id: str
    ) -> AbstractContextManager[None]: ...

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

    def create_release_review_decision_record_if_absent(
        self, record: ReleaseReviewDecisionRecord
    ) -> ReleaseReviewDecisionRecord: ...

    def record_release_review_decision_publication(
        self, *, record_id: str, release_issue_url: str
    ) -> ReleaseReviewDecisionRecord: ...

    def list_release_review_decision_records(
        self, *, product: str, limit: int | None = None
    ) -> tuple[ReleaseReviewDecisionRecord, ...]: ...

    def read_release_review_decision_record(
        self, *, product: str, record_id: str
    ) -> ReleaseReviewDecisionRecord: ...


RELEASE_RECORD_PENDING = (
    "The decision is saved, but the release record could not be published. Try recording it again."
)
CLIENT_APPROVAL_REQUIRED = "Client approval of this release is required."

RELEASE_EVIDENCE_MESSAGES: dict[ReleaseEvidenceReason, str] = {
    "testing_lane_missing": "This product has no testing lane to release from.",
    "source_control_access_unavailable": "Launchplane cannot read this product's source control.",
    "production_identity_missing": "Production has no recorded deployed version.",
    "candidate_identity_missing": "Testing has no recorded deployed version.",
    "release_record_missing": "A release record Launchplane needs is missing.",
    "github_read_failed": "Reading this release's changes from GitHub failed.",
}

_LOGGER = logging.getLogger(__name__)


@dataclass(eq=False)
class ReleaseEvidenceUnavailable(ValueError):
    """The release checklist cannot be compiled, for one fixed, public-safe reason."""

    code: ReleaseEvidenceReason

    def __post_init__(self) -> None:
        super().__init__(RELEASE_EVIDENCE_MESSAGES[self.code])


def _identity_missing(instance: str) -> ReleaseEvidenceUnavailable:
    return ReleaseEvidenceUnavailable(
        "production_identity_missing" if instance == "prod" else "candidate_identity_missing"
    )


def checklist_digest(checklist: ReleaseChecklist) -> str:
    payload = checklist.model_dump(mode="json")
    # Derived engineering evidence is bound to the immutable artifact pair,
    # not an additional Client decision. Preserve existing exact-tuple acceptance.
    payload.pop("database_compatibility", None)
    # Prior preview decisions are helpful annotations, never release approval.
    for item in payload["items"]:
        item.pop("already_reviewed")
    for source in payload.get("shared_sources", ()):
        for item in source["items"]:
            item.pop("already_reviewed")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def release_version(
    *, store: ReleaseReviewStore, profile: LaunchplaneProductProfileRecord, instance: str
) -> ReleaseVersion:
    lane = next((lane for lane in profile.lanes if lane.instance == instance), None)
    if lane is None:
        if instance == "testing":
            raise ReleaseEvidenceUnavailable("testing_lane_missing")
        raise _identity_missing(instance)
    if profile.driver_id == "odoo":
        try:
            release = store.read_release_tuple_record(
                context_name=lane.context, channel_name=instance
            )
            artifact = store.read_artifact_manifest(release.artifact_id)
        except FileNotFoundError as error:
            raise ReleaseEvidenceUnavailable("release_record_missing") from error
        if release.context != lane.context or release.channel != instance:
            raise _identity_missing(instance)

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
        try:
            return ReleaseVersion(
                artifact_id=release.artifact_id,
                source_commit=artifact.source_commit,
                shared_addons_digest=shared_digest,
            )
        except ValueError as error:
            raise _identity_missing(instance) from error
    try:
        inventory = store.read_environment_inventory(
            context_name=lane.context, instance_name=instance
        )
    except FileNotFoundError as error:
        raise ReleaseEvidenceUnavailable("release_record_missing") from error
    identity = inventory.runtime_identity
    if (
        inventory.context != lane.context
        or inventory.instance != instance
        or identity is None
        or inventory.deploy.status != "pass"
        or identity.product != profile.product
        or identity.instance != instance
    ):
        raise _identity_missing(instance)
    try:
        return ReleaseVersion(
            artifact_id=identity.artifact_id, source_commit=identity.source_git_ref
        )
    except ValueError as error:
        raise _identity_missing(instance) from error


def build_release_review(
    *, store: ReleaseReviewStore, profile: LaunchplaneProductProfileRecord, read: GitHubRead
) -> ReleaseReviewStatus:
    production = release_version(store=store, profile=profile, instance="prod")
    candidate = release_version(store=store, profile=profile, instance="testing")
    lane = next(lane for lane in profile.lanes if lane.instance == "testing")
    shared_sources: tuple[SharedSourceReview, ...] = ()
    additional_changes: tuple[str, ...] = ()
    try:
        # Preview drivers write bare repository anchors; readers accept both
        # bare and owner/repo. Keep both within the product's preview context.
        preview_hosts = recorded_preview_hosts(
            (
                record
                for anchor in (profile.repository, profile.repository.rsplit("/", 1)[-1])
                for record in store.list_preview_records(
                    context_name=profile.preview.context, anchor_repo=anchor
                )
            ),
            repository=profile.repository,
        )
        items, untracked = read_release_changes(
            repository=profile.repository,
            production_commit=production.source_commit,
            candidate_commit=candidate.source_commit,
            read=read,
            preview_hosts=preview_hosts,
        )
        if production.shared_addons_digest != candidate.shared_addons_digest:
            shared_sources, additional_changes = read_shared_source_changes(
                production=store.read_artifact_manifest(production.artifact_id),
                candidate=store.read_artifact_manifest(candidate.artifact_id),
                repository=profile.repository,
                read=read,
                preview_hosts=preview_hosts,
            )
    except ReleaseEvidenceUnavailable:
        raise
    except (ValueError, click.ClickException) as error:
        raise ReleaseEvidenceUnavailable("github_read_failed") from error
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
        shared_sources=shared_sources,
        additional_changes=additional_changes,
        database_compatibility=(
            classify_release_database_compatibility(
                production=store.read_artifact_manifest(production.artifact_id),
                candidate=store.read_artifact_manifest(candidate.artifact_id),
            )
            if profile.driver_id == "odoo"
            else None
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
    complete = not blockers
    approved = bool(latest_decision and latest_decision.release_issue_url) and (
        bool(latest_decision and latest_decision.decision == "overridden")
        or bool(not blockers and latest_decision and latest_decision.decision == "accepted")
    )
    if latest_decision and not latest_decision.release_issue_url:
        blockers += (RELEASE_RECORD_PENDING,)
    if not approved and not blockers:
        blockers = (
            "The Client requested changes." if latest_decision else CLIENT_APPROVAL_REQUIRED,
        )
    return ReleaseReviewStatus(
        required=profile.production_use != "prelaunch",
        approved=approved,
        checklist_complete=complete,
        checklist=checklist,
        checklist_digest=digest,
        blockers=() if approved else blockers,
        latest_decision=latest_decision,
    )


def checklist_blockers(checklist: ReleaseChecklist) -> tuple[str, ...]:
    blockers = []
    if not checklist.owner_github_id:
        blockers.append("No Client set for this product.")
    if not checklist.testing_url:
        blockers.append("The testing site URL is unavailable.")
    groups = [("", checklist.items, checklist.untracked_commits)] + [
        (f"{source.repository} ", source.items, source.untracked_commits)
        for source in checklist.shared_sources
    ]
    for repository, items, untracked in groups:
        for item in items:
            if not item.owner_test_notes.strip():
                blockers.append(
                    f"Pull request {repository}#{item.pull_request_number} has no Client test notes."
                )
            # A merge-train batch PR names each of its pull requests that came without notes.
            for number in pull_requests_missing_owner_test_notes(item.owner_test_notes):
                blockers.append(
                    f"Pull request {repository}#{number}, landed in {repository}#{item.pull_request_number},"
                    " has no Client test notes."
                )
        if untracked:
            blockers.append(
                f"The release contains {repository}commits without a merged pull request and Client test notes."
            )
    blockers.extend(checklist.additional_changes)
    return tuple(blockers)


def unavailable_release_review(code: ReleaseEvidenceReason) -> ReleaseReviewStatus:
    return ReleaseReviewStatus(
        blockers=(
            f"The current release checklist is unavailable. {RELEASE_EVIDENCE_MESSAGES[code]}",
        ),
        unavailable_reason=code,
    )


def current_release_review(
    *,
    control_plane_root: Path,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    include_prelaunch: bool = False,
    trace_id: str = "",
) -> ReleaseReviewStatus:
    with ExitStack() as credentials:
        if profile.production_use == "prelaunch" and not include_prelaunch:
            return ReleaseReviewStatus(required=False, approved=True)
        try:
            lane = next((lane for lane in profile.lanes if lane.instance == "testing"), None)
            if lane is None:
                raise ReleaseEvidenceUnavailable("testing_lane_missing")
            tokens: dict[str, str] = {}

            def read(path: str) -> object:
                # Each repository uses its existing scoped App access, including shared sources.
                parts = path.split("/")
                repository = unquote("/".join(parts[2:4]))
                if repository not in tokens:
                    tokens[repository] = credentials.enter_context(
                        launchplane_github_token(
                            token_resolver=resolve_launchplane_github_token,
                            api_request=github_api_request,
                            control_plane_root=control_plane_root,
                            context_name=lane.context,
                            repository=repository,
                        )
                    )
                if not tokens[repository]:
                    raise ReleaseEvidenceUnavailable("source_control_access_unavailable")
                return github_api_request(path=path, token=tokens[repository])

            return build_release_review(
                store=cast(ReleaseReviewStore, record_store),
                profile=profile,
                read=read,
            )
        # Provider errors may contain private URLs: log and return only the fixed code.
        except ReleaseEvidenceUnavailable as error:
            return _unavailable(profile=profile, code=error.code, trace_id=trace_id, error=error)
        except click.ClickException as error:
            return _unavailable(
                profile=profile, code="github_read_failed", trace_id=trace_id, error=error
            )
        except (AttributeError, FileNotFoundError, ValueError) as error:
            return _unavailable(
                profile=profile, code="release_record_missing", trace_id=trace_id, error=error
            )


def _unavailable(
    *,
    profile: LaunchplaneProductProfileRecord,
    code: ReleaseEvidenceReason,
    trace_id: str,
    error: BaseException,
) -> ReleaseReviewStatus:
    _LOGGER.warning(
        "release checklist unavailable product=%s reason=%s trace_id=%s error_type=%s",
        profile.product,
        code,
        trace_id or "-",
        type(error.__cause__ or error).__name__,
    )
    return unavailable_release_review(code)


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
            " ".join(review.blockers) or "Client release approval is required."
        )
    candidate = review.checklist.candidate
    if (artifact_id and artifact_id != candidate.artifact_id) or (
        source_commit and source_commit != candidate.source_commit
    ):
        raise click.ClickException("Promotion no longer matches the approved testing release.")


class ProductionChangeRequiresPromotion(click.ClickException):
    """A direct deploy tried to change what a live production lane runs."""

    code = "promotion_required"


def require_unchanged_production_artifact(
    *,
    record_store: object,
    product: str,
    instance: str,
    artifact_id: str,
    deploy_reference: str = "",
) -> None:
    """Direct deploys may redeploy live production; changing it goes through promotion,
    which carries the release and backup gates. Rollback is the recovery path. An
    empty artifact_id redeploys what production runs."""
    if instance.strip().lower() != "prod" or not artifact_id:
        return
    store = cast(ReleaseReviewStore, record_store)
    profile = store.read_product_profile_record(product)
    if profile.production_use == "prelaunch":
        return
    try:
        production = release_version(store=store, profile=profile, instance="prod")
        deployed_image = production.artifact_id
        if deploy_reference.strip():
            # The provider deploys the tag, so it must name the image production runs.
            lane = next(lane for lane in profile.lanes if lane.instance == "prod")
            identity = store.read_environment_inventory(
                context_name=lane.context, instance_name="prod"
            ).runtime_identity
            deployed_image = identity.image_reference if identity is not None else ""
    except ReleaseEvidenceUnavailable as error:
        raise ProductionChangeRequiresPromotion(
            f"{error} A direct deploy cannot establish production; promote the release, "
            "or record the product as prelaunch to bootstrap it."
        ) from error
    except FileNotFoundError as error:
        raise ProductionChangeRequiresPromotion(
            "Production has no recorded deployed image; promote the release."
        ) from error
    requested_image = deploy_reference.strip() or artifact_id
    if artifact_id != production.artifact_id or requested_image != deployed_image:
        raise ProductionChangeRequiresPromotion(
            "A direct deploy cannot change the artifact production runs; "
            "promote the release, or roll back to recover."
        )
