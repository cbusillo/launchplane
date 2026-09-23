"""Human-only release decisions; recording an opinion never deploys anything."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, cast
from uuid import uuid4

import click
from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import (
    ReleaseDecision,
    ReleaseReviewDecisionRecord,
    ReleaseReviewStatus,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.product_review import viewer_is_product_owner
from control_plane.release_review import (
    RELEASE_RECORD_PENDING,
    ReleaseReviewStore,
    checklist_blockers,
)
from control_plane.service_auth import AuthorizationTarget, GitHubHumanIdentity, LaunchplaneIdentity


@dataclass(frozen=True, slots=True)
class ReleaseReviewRouteDependencies:
    common: ReadRouteDependencies
    read_github_human_browser_mutation_identity: Callable[..., GitHubHumanIdentity]
    current_review: Callable[[object, LaunchplaneProductProfileRecord], ReleaseReviewStatus]
    publish_decision: Callable[[LaunchplaneProductProfileRecord, ReleaseReviewDecisionRecord], str]


class ReleaseReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace_id: str
    product: str
    display_name: str
    owner_github_login: str
    viewer_is_owner: bool
    can_override: bool
    review: ReleaseReviewStatus


class ReleaseReviewDecisionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = Field(min_length=1, max_length=256)
    checklist_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ReleaseDecision
    reason: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def validate_reason(self) -> "ReleaseReviewDecisionEnvelope":
        self.reason = self.reason.strip()
        if self.decision != "accepted" and not self.reason:
            raise ValueError("Requesting changes or overriding requires a reason.")
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
            if candidate.driver_id == "verireel":
                actions += ("verireel_prod_promotion.execute",)
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

    def response(
        store: object,
        profile: LaunchplaneProductProfileRecord,
        identity: LaunchplaneIdentity,
        trace_id: str,
    ) -> ReleaseReviewResponse:
        return ReleaseReviewResponse(
            trace_id=trace_id,
            product=profile.product,
            display_name=profile.display_name,
            owner_github_login=profile.owner.github_login,
            viewer_is_owner=isinstance(identity, GitHubHumanIdentity)
            and viewer_is_product_owner(profile=profile, identity=identity),
            can_override=can_override(profile, identity),
            review=dependencies.current_review(store, profile),
        )

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
        allowed = (
            can_override(profile, identity)
            if envelope.decision == "overridden"
            else viewer_is_product_owner(profile=profile, identity=identity)
        )
        if not allowed:
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="release_decision_denied",
                message="You cannot make this release decision.",
            )
        current = response(record_store, profile, identity, trace_id)
        review = current.review
        if review.checklist is None or review.checklist_digest != envelope.checklist_digest:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="release_checklist_changed",
                message="The release checklist changed or is unavailable. Reload it before deciding.",
            )
        if envelope.decision == "accepted" and checklist_blockers(review.checklist):
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
            actor_github_id=str(identity.github_id),
            actor_github_login=identity.login,
            decided_at=datetime.now(UTC).isoformat(),
        )
        previous = review.latest_decision
        if (
            previous
            and not previous.release_issue_url
            and previous.actor_github_id == decision.actor_github_id
            and previous.decision == decision.decision
            and previous.reason == decision.reason
        ):
            decision = previous
        store = cast(ReleaseReviewStore, record_store)
        store.write_release_review_decision_record(decision)
        try:
            issue_url = dependencies.publish_decision(profile, decision)
        except (AttributeError, FileNotFoundError, StopIteration, ValueError, click.ClickException):
            issue_url = ""
        if issue_url:
            decision = decision.model_copy(update={"release_issue_url": issue_url})
            store.write_release_review_decision_record(decision)
        blockers: tuple[str, ...] = (
            ("The Owner requested changes.",) if decision.decision == "changes_requested" else ()
        )
        if not issue_url:
            blockers += (RELEASE_RECORD_PENDING,)
        return current.model_copy(
            update={
                "review": review.model_copy(
                    update={
                        "latest_decision": decision,
                        "approved": bool(issue_url) and decision.decision != "changes_requested",
                        "blockers": blockers,
                    }
                )
            }
        )

    for path, endpoint, method, operation in (
        ("/v1/release-review", read_release_review, "GET", "read_release_review"),
        (
            "/v1/release-review/decisions",
            write_release_review_decision,
            "POST",
            "write_release_review_decision",
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
