"""The small Owner product-review path.

Authorization is one question: is the signed-in GitHub user the Owner named on the
product record? A decision is a recorded opinion and never merges or deploys.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol, cast
from uuid import uuid4

from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_review import (
    ProductReviewDecision,
    ProductReviewDecisionRecord,
)
from control_plane.service_auth import GitHubHumanIdentity


class ProductReviewStore(Protocol):
    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...

    def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord: ...

    def write_product_review_decision_record(
        self, record: ProductReviewDecisionRecord
    ) -> object: ...

    def list_product_review_decision_records(
        self,
        *,
        repository: str,
        pull_request_number: int,
        limit: int | None = None,
    ) -> tuple[ProductReviewDecisionRecord, ...]: ...


_REQUIRED_STORE_METHODS = (
    "list_product_profile_records",
    "list_preview_records",
    "read_preview_generation_record",
    "write_product_review_decision_record",
    "list_product_review_decision_records",
)


def require_product_review_store(record_store: object) -> ProductReviewStore:
    missing_methods = [
        method_name
        for method_name in _REQUIRED_STORE_METHODS
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        raise TypeError(
            "Launchplane record store does not support product review: "
            + ", ".join(missing_methods)
        )
    return cast(ProductReviewStore, record_store)


@dataclass(frozen=True, slots=True)
class ProductReviewPreview:
    preview_url: str = ""
    head_sha: str = ""


def source_control_pull_request_url(*, repository: str, pull_request_number: int) -> str:
    # GitHub is the current source-control adapter; this is the one place that knows it.
    return f"https://github.com/{repository}/pull/{pull_request_number}"


def product_profiles_for_repository(
    *, store: ProductReviewStore, repository: str
) -> tuple[LaunchplaneProductProfileRecord, ...]:
    normalized_repository = repository.strip().casefold()
    return tuple(
        profile
        for profile in store.list_product_profile_records()
        if profile.repository.strip().casefold() == normalized_repository
    )


def viewer_is_product_owner(
    *, profile: LaunchplaneProductProfileRecord, identity: GitHubHumanIdentity
) -> bool:
    return profile.owner.is_set and profile.owner.github_id == str(identity.github_id)


def resolve_serving_preview(
    *,
    store: ProductReviewStore,
    profile: LaunchplaneProductProfileRecord,
    pull_request_number: int,
) -> ProductReviewPreview:
    """Return the one active preview that is serving a ready generation, if any."""

    if not profile.preview.enabled or not profile.preview.context.strip():
        return ProductReviewPreview()
    repository = profile.repository.strip().casefold()
    accepted_anchor_repositories = {repository, repository.split("/", 1)[-1]}
    serving: list[ProductReviewPreview] = []
    for preview in store.list_preview_records(
        context_name=profile.preview.context,
        anchor_pr_number=pull_request_number,
    ):
        if (
            preview.state != "active"
            or not preview.serving_generation_id.strip()
            or preview.anchor_repo.strip().casefold() not in accepted_anchor_repositories
        ):
            continue
        try:
            generation = store.read_preview_generation_record(preview.serving_generation_id)
        except FileNotFoundError:
            continue
        if generation.preview_id != preview.preview_id or generation.state != "ready":
            continue
        serving.append(
            ProductReviewPreview(
                preview_url=preview.canonical_url.strip(),
                head_sha=generation.anchor_summary.head_sha.strip().lower(),
            )
        )
    if len(serving) != 1:
        return ProductReviewPreview()
    return serving[0]


def latest_product_review_decision(
    *,
    store: ProductReviewStore,
    profile: LaunchplaneProductProfileRecord,
    pull_request_number: int,
) -> ProductReviewDecisionRecord | None:
    records = store.list_product_review_decision_records(
        repository=profile.repository,
        pull_request_number=pull_request_number,
        limit=1,
    )
    return records[0] if records else None


def record_product_review_decision(
    *,
    store: ProductReviewStore,
    profile: LaunchplaneProductProfileRecord,
    pull_request_number: int,
    preview: ProductReviewPreview,
    decision: ProductReviewDecision,
    reason: str,
    identity: GitHubHumanIdentity,
) -> ProductReviewDecisionRecord:
    decided_at = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    record = ProductReviewDecisionRecord(
        record_id=f"product-review-{profile.product}-pr-{pull_request_number}-{uuid4().hex}",
        product=profile.product,
        repository=profile.repository,
        pull_request_number=pull_request_number,
        head_sha=preview.head_sha,
        preview_url=preview.preview_url,
        decision=decision,
        reason=reason.strip(),
        owner_github_id=str(identity.github_id),
        owner_github_login=identity.login,
        decided_at=decided_at.replace("+00:00", "Z"),
    )
    store.write_product_review_decision_record(record)
    return record
