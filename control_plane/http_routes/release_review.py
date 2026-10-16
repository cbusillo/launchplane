"""Human-only release decisions.

Recording a decision deploys nothing by itself. A Client's acceptance of a product
whose releases are not held is stamped with how its release runs, and Launchplane's
worker then promotes it through the gated path (``control_plane.client_release``).

Only the Director, the signed-in person the active policy names as administrator,
records a temporary hotfix; the worker promotes it the same way. Once it is live the
Client reviews that exact checklist, and a request for changes rolls it back.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import uuid4
from pathlib import Path

import click
from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.client_release import (
    ClientReleaseRunView,
    live_temporary_hotfix,
    read_client_release_run,
    rejection_rollback_pending,
    rejection_rollback_target,
    release_start_for_acceptance,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ReleaseOnAcceptance,
)
from control_plane.contracts.release_review import (
    ReleaseDecision,
    ReleaseReviewDecisionRecord,
    ReleaseReviewStatus,
    ReleaseStart,
)
from control_plane.durable_operation_authorization import read_active_authz_policy_record
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.product_review import viewer_is_product_owner
from control_plane.release_review import (
    RELEASE_RECORD_PENDING,
    ReleaseReviewStore,
    checklist_blockers,
)
from control_plane.service_auth import AuthorizationTarget, GitHubHumanIdentity, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.odoo_business_hours import BusinessHoursWindow
from control_plane.odoo_business_hours import read_business_hours_window


@dataclass(frozen=True, slots=True)
class ReleaseReviewRouteDependencies:
    common: ReadRouteDependencies
    read_github_human_browser_mutation_identity: Callable[..., GitHubHumanIdentity]
    current_review: Callable[[object, LaunchplaneProductProfileRecord, str], ReleaseReviewStatus]
    publish_decision: Callable[
        [ReleaseReviewStore, LaunchplaneProductProfileRecord, ReleaseReviewDecisionRecord], str
    ]
    # Opens the Client's release request for an engineering-only release, with
    # the admin's reason for the reviewed checklist digest; returns whether a
    # request is open afterwards.
    open_urgent_review: Callable[
        [ReleaseReviewStore, LaunchplaneProductProfileRecord, str, str], bool
    ]
    control_plane_root: Path | None = None
    adopt_invitation: (
        Callable[[ReleaseReviewStore, LaunchplaneProductProfileRecord, str, int, int, bool], bool]
        | None
    ) = None


class ReleaseReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    product: str
    display_name: str
    owner_github_login: str
    viewer_is_owner: bool
    can_override: bool
    review: ReleaseReviewStatus
    # Whether accepting starts the release, the site it goes live on, and what the
    # newest accepted release has done so far.
    release_on_acceptance: ReleaseOnAcceptance = "held"
    live_site_url: str = ""
    release_run: ClientReleaseRunView | None = None
    # Whether the viewer is the Director and may ship a temporary hotfix, and whether
    # the review shown is a live temporary hotfix awaiting the Client.
    can_start_temporary_hotfix: bool = False
    reviewing_temporary_hotfix: bool = False
    business_hours: BusinessHoursWindow | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class ReleaseReviewDecisionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1, max_length=256)
    checklist_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ReleaseDecision
    reason: str = Field(default="", max_length=4000)
    release_during_business_hours: bool = False

    @model_validator(mode="after")
    def validate_reason(self) -> "ReleaseReviewDecisionEnvelope":
        self.reason = self.reason.strip()
        if self.release_during_business_hours and (
            self.decision not in {"accepted", "temporary_hotfix"} or not self.reason
        ):
            raise ValueError(
                "Releasing during business hours requires acceptance or a hotfix and a reason."
            )
        if self.decision != "accepted" and not self.reason:
            raise ValueError("Requesting changes or overriding requires a reason.")
        return self


class UrgentReleaseReviewEnvelope(BaseModel):
    """An admin's request to ask the Client about an engineering-only release now."""

    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1, max_length=256)
    checklist_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str = Field(min_length=1, max_length=1000)
    adopt_issue_number: int | None = Field(default=None, ge=1)
    adopt_comment_id: int | None = Field(default=None, ge=1)
    dry_run: bool = False

    @model_validator(mode="after")
    def validate_reason(self) -> "UrgentReleaseReviewEnvelope":
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("Opening an urgent release review requires a reason.")
        if (self.adopt_issue_number is None) != (self.adopt_comment_id is None):
            raise ValueError("Receipt adoption requires both the issue and original comment.")
        if self.dry_run and self.adopt_issue_number is None:
            raise ValueError("Dry run is supported for receipt adoption only.")
        return self


def register_release_review_routes(
    app: ApiRouteRegistrar, *, dependencies: ReleaseReviewRouteDependencies
) -> None:
    common = dependencies.common

    def profile_for_viewer(
        store: object, product: str, identity: LaunchplaneIdentity, trace_id: str
    ) -> LaunchplaneProductProfileRecord:
        profile = None
        try:
            candidate = cast(ReleaseReviewStore, store).read_product_profile_record(product)
            owner = isinstance(identity, GitHubHumanIdentity) and viewer_is_product_owner(
                profile=candidate, identity=identity
            )
            lane = next((lane for lane in candidate.lanes if lane.instance == "prod"), None)
            actions: tuple[str, ...] = (
                ("odoo_prod_promotion.execute", "odoo_prod_promotion_run.execute")
                if candidate.driver_id == "odoo"
                else ("generic_web_prod_promotion.execute", "generic_web_prod_promotion.dispatch")
            )
            promotion_reader = lane is not None and any(
                common.authorization_allows(
                    identity=identity,
                    action=action,
                    product=product,
                    context=lane.context,
                    target=AuthorizationTarget(scope="instance", instances=("testing", "prod")),
                )
                for action in actions
            )
            if candidate.is_active and (
                owner
                or promotion_reader
                or common.authorization_allows(
                    identity=identity,
                    action="product_profile.read",
                    product=product,
                    context="launchplane",
                )
            ):
                profile = candidate
        except FileNotFoundError:
            pass
        if profile is None:
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="release_review_unavailable",
                message="This release review is unavailable to you.",
            )
        return profile

    def can_override(
        profile: LaunchplaneProductProfileRecord, identity: LaunchplaneIdentity
    ) -> bool:
        return common.authorization_allows(
            identity=identity,
            action="product_profile.write",
            product=profile.product,
            context="launchplane",
        )

    def is_director(store: object, identity: LaunchplaneIdentity) -> bool:
        if not isinstance(identity, GitHubHumanIdentity) or not isinstance(
            store, PostgresRecordStore
        ):
            return False
        try:
            policy = read_active_authz_policy_record(store).policy
        except (LookupError, TypeError):
            return False
        return policy.names_administrator(identity)

    def hotfix_supported(profile: LaunchplaneProductProfileRecord) -> bool:
        # The gated release worker runs Odoo and generic web; a prelaunch product has
        # no release gate, and standing acceptance already releases the Director's own.
        return (
            profile.driver_id in {"odoo", "generic-web"}
            and profile.production_use != "prelaunch"
            and profile.release_on_acceptance != "director_standing"
            and profile.owner.is_set
            and any(lane.instance == "prod" for lane in profile.lanes)
        )

    def release_run(
        store: object,
        profile: LaunchplaneProductProfileRecord,
        review: ReleaseReviewStatus,
        *,
        reviewing_hotfix: bool,
    ) -> ClientReleaseRunView | None:
        latest = cast(ReleaseReviewStore, store).list_release_review_decision_records(
            product=profile.product, limit=1
        )
        if not latest:
            return None
        decision = latest[0]
        if not reviewing_hotfix and decision != review.latest_decision:
            checklist = review.checklist
            # A completed release changes the production side of the checklist's
            # digest. Keep its progress only while both lanes carry its candidate;
            # a new testing invitation must never inherit that historical run.
            if (
                checklist is None
                or checklist.candidate != decision.checklist.candidate
                or checklist.production != checklist.candidate
            ):
                return None
        return read_client_release_run(
            store=store,
            profile=profile,
            decision=decision,
            control_plane_root=dependencies.control_plane_root,
        )

    def response(
        store: object,
        profile: LaunchplaneProductProfileRecord,
        identity: LaunchplaneIdentity,
        trace_id: str,
    ) -> ReleaseReviewResponse:
        prod_lane = next((lane for lane in profile.lanes if lane.instance == "prod"), None)
        viewer_is_owner = isinstance(identity, GitHubHumanIdentity) and viewer_is_product_owner(
            profile=profile, identity=identity
        )
        hotfix = live_temporary_hotfix(store, profile)
        director = (
            not viewer_is_owner and hotfix_supported(profile) and is_director(store, identity)
        )
        # The Client reviews the live hotfix's exact checklist; the Director keeps seeing
        # what testing carries now, so a further fix binds the right candidate.
        newest = (
            cast(ReleaseReviewStore, store).list_release_review_decision_records(
                product=profile.product, limit=1
            )
            if hotfix is not None
            else ()
        )
        review = (
            ReleaseReviewStatus(
                approved=True,
                checklist_complete=True,
                checklist=hotfix.checklist,
                checklist_digest=hotfix.checklist_digest,
                # The hotfix, or the Client's rejection of it awaiting publication.
                latest_decision=newest[0] if newest else hotfix,
            )
            if hotfix is not None and not director
            else dependencies.current_review(store, profile, trace_id)
        )
        return ReleaseReviewResponse(
            trace_id=trace_id,
            product=profile.product,
            display_name=profile.display_name,
            owner_github_login=profile.owner.github_login,
            viewer_is_owner=viewer_is_owner,
            can_override=can_override(profile, identity),
            review=review,
            release_on_acceptance=release_start_for_acceptance(store=store, profile=profile)
            or "held",
            live_site_url=prod_lane.base_url if prod_lane is not None else "",
            release_run=release_run(store, profile, review, reviewing_hotfix=hotfix is not None),
            can_start_temporary_hotfix=director,
            reviewing_temporary_hotfix=hotfix is not None,
            business_hours=(
                read_business_hours_window(
                    store=store, profile=profile, control_plane_root=dependencies.control_plane_root
                )
                if profile.production_use == "live" and profile.odoo_business_hours
                else None
            ),
        )

    def decision_release_start(
        store: object,
        profile: LaunchplaneProductProfileRecord,
        decision: ReleaseDecision,
        current: ReleaseReviewResponse,
    ) -> ReleaseStart:
        if decision == "temporary_hotfix":
            return "promote"
        if current.reviewing_temporary_hotfix:
            # The hotfix is already live: Accept keeps it; a request for changes rolls back.
            return "rollback" if decision == "changes_requested" else ""
        if decision == "accepted":
            return release_start_for_acceptance(store=store, profile=profile)
        return ""

    def read_release_review(
        product: Annotated[str, Query(min_length=1, max_length=256)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ReleaseReviewResponse:
        trace_id = common.next_trace_id()
        profile = profile_for_viewer(record_store, product, identity, trace_id)
        return response(record_store, profile, identity, trace_id)

    def write_release_review_decision(
        envelope: ReleaseReviewDecisionEnvelope,
        identity: Annotated[
            GitHubHumanIdentity, Depends(dependencies.read_github_human_browser_mutation_identity)
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ReleaseReviewResponse:
        trace_id = common.next_trace_id()
        profile = profile_for_viewer(record_store, envelope.product, identity, trace_id)
        hotfix = envelope.decision == "temporary_hotfix"
        allowed = (
            can_override(profile, identity)
            if envelope.decision == "overridden"
            # Never an agent, a workflow, another admin role or the Client: only the
            # person the active policy names as administrator.
            else (
                not viewer_is_product_owner(profile=profile, identity=identity)
                and is_director(record_store, identity)
            )
            if hotfix
            else viewer_is_product_owner(profile=profile, identity=identity)
        )
        if not allowed:
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="temporary_hotfix_denied" if hotfix else "release_decision_denied",
                message="Only the Director can start a temporary hotfix."
                if hotfix
                else "You cannot make this release decision.",
            )
        current = response(record_store, profile, identity, trace_id)
        latest = cast(ReleaseReviewStore, record_store).list_release_review_decision_records(
            product=profile.product, limit=1
        )
        if (
            not hotfix
            and latest
            and latest[0].decision == "temporary_hotfix"
            and latest[0].release_issue_url
            and current.release_run is not None
            and current.release_run.state in {"waiting", "running"}
        ):
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="temporary_hotfix_in_progress",
                message="An urgent temporary fix is going live now. Reload in a few minutes to review it.",
            )
        if rejection_rollback_pending(record_store, profile):
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="temporary_hotfix_rollback_in_progress",
                message="The Client's request for changes is rolling the site back, or its rollback stopped and needs an admin rollback. Reload once production has left the rejected fix.",
            )
        if current.reviewing_temporary_hotfix and envelope.decision == "overridden":
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="temporary_hotfix_awaiting_client",
                message="The live temporary hotfix waits for the Client's review.",
            )
        review = current.review
        if review.no_new_release:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="no_new_release",
                message="Production already runs the testing version. There is no new release to decide.",
            )
        if hotfix:
            refusal = ""
            if not hotfix_supported(profile):
                refusal = "This product has no gated release path for a temporary hotfix."
            elif current.release_run is not None and current.release_run.state == "running":
                # A waiting release has admitted nothing; this newer decision stops it.
                refusal = "A release step is still running. Wait for it to finish."
            elif (
                rejection_rollback_target(cast(PostgresRecordStore, record_store), profile) is None
            ):
                refusal = "No Client-accepted release has a passing production deployment to roll back to. Use an admin override and hand promotion instead."
            if refusal:
                raise common.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="temporary_hotfix_unavailable",
                    message=refusal,
                )
            if (
                review.checklist is not None
                and review.checklist.production == review.checklist.candidate
            ):
                raise common.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="temporary_hotfix_unavailable",
                    message="Testing carries the version production already runs.",
                )
        if review.checklist is None or review.checklist_digest != envelope.checklist_digest:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="release_checklist_changed",
                message="The release checklist changed or is unavailable. Reload it before deciding.",
            )
        if envelope.decision in ("accepted", "temporary_hotfix") and checklist_blockers(
            review.checklist
        ):
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="release_checklist_incomplete",
                message="The checklist is incomplete. Resolve its blockers before accepting.",
            )
        decision = ReleaseReviewDecisionRecord(
            record_id=f"release-review-{uuid4().hex}",
            product=profile.product,
            checklist_digest=review.checklist_digest,
            checklist=review.checklist,
            decision=envelope.decision,
            reason=envelope.reason,
            release_during_business_hours=envelope.release_during_business_hours,
            actor_github_id=str(identity.github_id),
            actor_github_login=identity.login,
            decided_at=datetime.now(UTC).isoformat(),
            # Fixed now: holding releases later stops the run but never edits this.
            release_start=decision_release_start(record_store, profile, envelope.decision, current),
        )
        previous = review.latest_decision
        store = cast(ReleaseReviewStore, record_store)
        repeated = bool(
            previous
            and previous.actor_github_id == decision.actor_github_id
            and previous.decision == decision.decision
            and previous.reason == decision.reason
            and previous.release_start == decision.release_start
            and previous.release_during_business_hours == decision.release_during_business_hours
            and (current.release_run is None or current.release_run.state != "stopped")
        )
        if repeated and previous and not previous.release_issue_url:
            decision = previous
        newest = store.list_release_review_decision_records(product=profile.product, limit=1)
        if (
            repeated
            and previous
            and previous.release_issue_url
            and newest
            and newest[0].record_id == previous.record_id
        ):
            # Repeating the newest published decision records nothing new, so a second
            # click never replaces the acceptance a running release depends on.
            decision = previous
            issue_url = previous.release_issue_url
        else:
            decision = store.create_release_review_decision_record_if_absent(decision)
            issue_url = decision.release_issue_url
            if not issue_url:
                try:
                    issue_url = dependencies.publish_decision(store, profile, decision)
                except (
                    AttributeError,
                    FileNotFoundError,
                    StopIteration,
                    ValueError,
                    click.ClickException,
                ):
                    issue_url = ""
        if issue_url and not decision.release_issue_url:
            decision = store.record_release_review_decision_publication(
                record_id=decision.record_id, release_issue_url=issue_url
            )
            issue_url = decision.release_issue_url
        blockers: tuple[str, ...] = (
            ("The Client requested changes.",) if decision.decision == "changes_requested" else ()
        )
        if not issue_url:
            blockers += (RELEASE_RECORD_PENDING,)
        return current.model_copy(
            update={
                "release_run": read_client_release_run(
                    store=record_store,
                    profile=profile,
                    decision=decision,
                    control_plane_root=dependencies.control_plane_root,
                ),
                "review": review.model_copy(
                    update={
                        "latest_decision": decision,
                        "approved": bool(issue_url) and decision.decision != "changes_requested",
                        "blockers": blockers,
                    }
                ),
            }
        )

    def open_urgent_release_review(
        envelope: UrgentReleaseReviewEnvelope,
        identity: Annotated[
            GitHubHumanIdentity, Depends(dependencies.read_github_human_browser_mutation_identity)
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ReleaseReviewResponse:
        """Ask the Client now; their acceptance is still what starts the release."""
        trace_id = common.next_trace_id()
        profile = profile_for_viewer(record_store, envelope.product, identity, trace_id)
        if not can_override(profile, identity):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="release_review_open_denied",
                message="Only an admin can open an urgent release review.",
            )
        current = response(record_store, profile, identity, trace_id)
        if current.review.checklist_digest != envelope.checklist_digest:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="release_checklist_changed",
                message="The release checklist changed or is unavailable. Reload it before deciding.",
            )
        try:
            opened = (
                dependencies.adopt_invitation(
                    cast(ReleaseReviewStore, record_store),
                    profile,
                    envelope.checklist_digest,
                    envelope.adopt_issue_number,
                    envelope.adopt_comment_id,
                    envelope.dry_run,
                )
                if envelope.adopt_issue_number is not None
                and envelope.adopt_comment_id is not None
                and dependencies.adopt_invitation is not None
                else False
                if envelope.adopt_issue_number is not None
                else dependencies.open_urgent_review(
                    cast(ReleaseReviewStore, record_store),
                    profile,
                    envelope.reason,
                    envelope.checklist_digest,
                )
            )
        except (FileNotFoundError, StopIteration, ValueError, click.ClickException):
            opened = False
        if not opened:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="release_review_not_opened",
                message="The release review could not be confirmed. Reload before trying again; a request may already be open. The release must be complete, undecided and waiting only for the Client.",
            )
        return current

    for path, endpoint, method, operation in (
        ("/v1/release-review", read_release_review, "GET", "read_release_review"),
        (
            "/v1/release-review/decisions",
            write_release_review_decision,
            "POST",
            "write_release_review_decision",
        ),
        (
            "/v1/release-review/urgent-openings",
            open_urgent_release_review,
            "POST",
            "open_urgent_release_review",
        ),
    ):
        app.add_api_route(
            path,
            endpoint,
            methods=[method],
            response_model=ReleaseReviewResponse,
            tags=["release-review"],
            operation_id=operation,
            responses={
                status: {"model": common.error_response_model} for status in (401, 403, 409, 503)
            },
        )
