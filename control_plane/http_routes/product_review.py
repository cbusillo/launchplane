from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Never

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_review import (
    PRODUCT_REVIEW_REASON_MAX_LENGTH,
    ProductReviewDecision,
    ProductReviewDecisionRecord,
)
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    ReadRouteDependencies,
)
from control_plane.product_review import (
    ProductReviewStore,
    latest_product_review_decision,
    product_profiles_for_repository,
    record_product_review_decision,
    require_product_review_store,
    resolve_serving_preview,
    source_control_pull_request_url,
    viewer_is_product_owner,
)
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneIdentity
from control_plane.product_review_feedback import validate_owner_feedback_decision


PRODUCT_REVIEW_ROUTE = "/v1/product-review"
PRODUCT_REVIEW_DECISIONS_ROUTE = "/v1/product-review/decisions"
PRODUCT_REVIEW_FEEDBACK_RETRY_ROUTE = "/v1/product-review/feedback/retry"

_NO_OWNER_REASON = "No Owner set for this product"
_NOT_OWNER_REASON = "You are not this product's Owner."
_NO_PREVIEW_REASON = "No preview is ready for this pull request yet."


@dataclass(frozen=True, slots=True)
class ProductReviewRouteDependencies:
    common: ReadRouteDependencies
    read_github_human_browser_mutation_identity: Callable[..., GitHubHumanIdentity]
    # Best-effort and non-raising: the recorded decision never depends on it.
    publish_owner_review_status: Callable[
        [ProductReviewStore, LaunchplaneProductProfileRecord, int], object
    ]


class ProductReviewDecisionEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=3, max_length=256, pattern=r"^[^/\s]+/[^/\s]+$")
    pull_request: int = Field(ge=1)
    decision: ProductReviewDecision
    reason: str = Field(default="", max_length=PRODUCT_REVIEW_REASON_MAX_LENGTH)

    @model_validator(mode="after")
    def _validate_reason(self) -> "ProductReviewDecisionEnvelope":
        self.reason = self.reason.strip()
        if self.decision == "changes_requested" and not self.reason:
            raise ValueError("Requesting changes requires a reason.")
        return self


class ProductReviewFeedbackRetryEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(min_length=3, max_length=256, pattern=r"^[^/\s]+/[^/\s]+$")
    pull_request: int = Field(ge=1)
    decision_id: str = Field(min_length=1, max_length=512)


class ProductReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    trace_id: str
    product: str
    display_name: str
    repository: str
    pull_request_number: int
    pull_request_url: str
    preview_url: str
    head_sha: str
    owner_set: bool
    owner_github_login: str
    viewer_is_owner: bool
    can_decide: bool
    cannot_decide_reason: str
    latest_decision: ProductReviewDecisionRecord | None


def register_product_review_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ProductReviewRouteDependencies,
) -> None:
    common = dependencies.common

    def unavailable(trace_id: str) -> Never:
        # One closed answer: it never says whether the product or pull request exists.
        raise common.http_error(
            status_code=403,
            trace_id=trace_id,
            code="product_review_unavailable",
            message="This product review is unavailable to you.",
        )

    def review_store(record_store: object, trace_id: str) -> ProductReviewStore:
        try:
            return require_product_review_store(record_store)
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

    def visible_profile(
        *,
        store: ProductReviewStore,
        repository: str,
        identity: LaunchplaneIdentity,
        trace_id: str,
    ) -> LaunchplaneProductProfileRecord:
        for profile in product_profiles_for_repository(store=store, repository=repository):
            if viewer_is_product_owner(
                profile=profile, identity=identity
            ) or common.authorization_allows(
                identity=identity,
                action="product_profile.read",
                product=profile.product,
                context=LAUNCHPLANE_SERVICE_CONTEXT,
            ):
                return profile
        return unavailable(trace_id)

    def saved_decision(
        store: ProductReviewStore,
        profile: LaunchplaneProductProfileRecord,
        pull_request_number: int,
        decision_id: str,
        trace_id: str,
    ) -> ProductReviewDecisionRecord:
        for record in store.list_product_review_decision_records(
            repository=profile.repository, pull_request_number=pull_request_number
        ):
            if record.record_id == decision_id:
                return record
        raise common.http_error(
            status_code=404,
            trace_id=trace_id,
            code="product_review_decision_not_found",
            message="This saved review decision was not found.",
        )

    def build_response(
        *,
        store: ProductReviewStore,
        profile: LaunchplaneProductProfileRecord,
        identity: LaunchplaneIdentity,
        pull_request_number: int,
        trace_id: str,
        decision_id: str = "",
    ) -> ProductReviewResponse:
        preview = resolve_serving_preview(
            store=store, profile=profile, pull_request_number=pull_request_number
        )
        viewer_is_owner = viewer_is_product_owner(profile=profile, identity=identity)
        if not profile.owner.is_set:
            cannot_decide_reason = _NO_OWNER_REASON
        elif not viewer_is_owner:
            cannot_decide_reason = _NOT_OWNER_REASON
        elif not preview.preview_url:
            cannot_decide_reason = _NO_PREVIEW_REASON
        else:
            cannot_decide_reason = ""
        return ProductReviewResponse(
            trace_id=trace_id,
            product=profile.product,
            display_name=profile.display_name,
            repository=profile.repository,
            pull_request_number=pull_request_number,
            pull_request_url=source_control_pull_request_url(
                repository=profile.repository, pull_request_number=pull_request_number
            ),
            preview_url=preview.preview_url,
            head_sha=preview.head_sha,
            owner_set=profile.owner.is_set,
            owner_github_login=profile.owner.github_login,
            viewer_is_owner=viewer_is_owner,
            can_decide=not cannot_decide_reason,
            cannot_decide_reason=cannot_decide_reason,
            latest_decision=(
                saved_decision(store, profile, pull_request_number, decision_id, trace_id)
                if decision_id
                else latest_product_review_decision(
                    store=store, profile=profile, pull_request_number=pull_request_number
                )
            ),
        )

    def read_product_review(
        repository: Annotated[
            str,
            Query(min_length=3, max_length=256, pattern=r"^[^/\s]+/[^/\s]+$"),
        ],
        pull_request: Annotated[int, Query(ge=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        decision_id: Annotated[str, Query(max_length=512)] = "",
    ) -> ProductReviewResponse:
        trace_id = common.next_trace_id()
        store = review_store(record_store, trace_id)
        profile = visible_profile(
            store=store, repository=repository, identity=identity, trace_id=trace_id
        )
        return build_response(
            store=store,
            profile=profile,
            identity=identity,
            pull_request_number=pull_request,
            trace_id=trace_id,
            decision_id=decision_id,
        )

    def retry_product_review_feedback(
        envelope: ProductReviewFeedbackRetryEnvelope,
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_browser_mutation_identity),
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductReviewResponse:
        trace_id = common.next_trace_id()
        store = review_store(record_store, trace_id)
        profile = visible_profile(
            store=store, repository=envelope.repository, identity=identity, trace_id=trace_id
        )
        if not viewer_is_product_owner(profile=profile, identity=identity):
            unavailable(trace_id)
        with store.product_review_lock(
            repository=profile.repository, pull_request_number=envelope.pull_request
        ):
            decision = saved_decision(
                store, profile, envelope.pull_request, envelope.decision_id, trace_id
            )
            try:
                validate_owner_feedback_decision(decision)
            except ValueError as error:
                raise common.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="product_review_feedback_unavailable",
                    message="This saved decision cannot be delivered. Open the latest review to record a new decision.",
                ) from error
            if not decision.feedback_requested:
                store.write_product_review_decision_record(
                    decision.model_copy(update={"feedback_requested": True})
                )
        # Reconcile delivery only. Never record a new decision or require a live preview.
        dependencies.publish_owner_review_status(store, profile, envelope.pull_request)
        return build_response(
            store=store,
            profile=profile,
            identity=identity,
            pull_request_number=envelope.pull_request,
            trace_id=trace_id,
            decision_id=envelope.decision_id,
        )

    def write_product_review_decision(
        envelope: ProductReviewDecisionEnvelope,
        identity: Annotated[
            GitHubHumanIdentity,
            Depends(dependencies.read_github_human_browser_mutation_identity),
        ],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ProductReviewResponse:
        trace_id = common.next_trace_id()
        store = review_store(record_store, trace_id)
        profile = visible_profile(
            store=store, repository=envelope.repository, identity=identity, trace_id=trace_id
        )
        if not profile.owner.is_set:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="product_owner_not_set",
                message=_NO_OWNER_REASON,
            )
        if not viewer_is_product_owner(profile=profile, identity=identity):
            unavailable(trace_id)
        preview = resolve_serving_preview(
            store=store, profile=profile, pull_request_number=envelope.pull_request
        )
        if not preview.preview_url:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="product_review_preview_unavailable",
                message=_NO_PREVIEW_REASON,
            )
        record_product_review_decision(
            store=store,
            profile=profile,
            pull_request_number=envelope.pull_request,
            preview=preview,
            decision=envelope.decision,
            reason=envelope.reason,
            identity=identity,
        )
        dependencies.publish_owner_review_status(store, profile, envelope.pull_request)
        return build_response(
            store=store,
            profile=profile,
            identity=identity,
            pull_request_number=envelope.pull_request,
            trace_id=trace_id,
        )

    app.add_api_route(
        PRODUCT_REVIEW_ROUTE,
        read_product_review,
        methods=["GET"],
        response_model=ProductReviewResponse,
        tags=["product-review"],
        operation_id="read_product_review",
        summary="Read the Owner review page for one pull request",
        responses={
            status: {"model": common.error_response_model} for status in (401, 403, 404, 503)
        },
    )
    app.add_api_route(
        PRODUCT_REVIEW_FEEDBACK_RETRY_ROUTE,
        retry_product_review_feedback,
        methods=["POST"],
        response_model=ProductReviewResponse,
        tags=["product-review"],
        operation_id="retry_product_review_feedback",
        summary="Retry delivery of a saved Owner decision without recording a new decision",
        responses={
            status: {"model": common.error_response_model} for status in (401, 403, 404, 409, 503)
        },
    )
    app.add_api_route(
        PRODUCT_REVIEW_DECISIONS_ROUTE,
        write_product_review_decision,
        methods=["POST"],
        response_model=ProductReviewResponse,
        tags=["product-review"],
        operation_id="write_product_review_decision",
        summary="Record the product Owner's accept or request-changes decision",
        responses={
            status: {"model": common.error_response_model} for status in (401, 403, 409, 503)
        },
    )
