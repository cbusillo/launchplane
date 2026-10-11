from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ProductReviewDecision = Literal["accepted", "changes_requested"]
ProductReviewCarryReason = Literal["merge_train_base_refresh", "unchanged_patch_base_update"]
PRODUCT_REVIEW_REASON_MAX_LENGTH = 4000


class ProductReviewCarry(BaseModel):
    """The source acceptance and immutable evidence for an unchanged feature patch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    head_sha: str
    reason: ProductReviewCarryReason = "merge_train_base_refresh"
    # The train's refresh records that produced each new head, oldest first.
    refresh_record_ids: tuple[str, ...] = ()
    comparison_base_sha: str = ""

    @model_validator(mode="after")
    def _validate_carry(self) -> "ProductReviewCarry":
        if not self.record_id.strip() or not self.head_sha.strip():
            raise ValueError("a carried decision names the decision and head it came from")
        if self.reason == "merge_train_base_refresh" and not self.refresh_record_ids:
            raise ValueError("a train refresh carry names its refresh records")
        if self.reason == "unchanged_patch_base_update" and (
            len(self.comparison_base_sha) != 40
            or any(character not in "0123456789abcdef" for character in self.comparison_base_sha)
        ):
            raise ValueError("an unchanged-patch carry names its immutable comparison base")
        return self


class ProductReviewDecisionRecord(BaseModel):
    """One Client decision about one pull request preview.

    The decision is a recorded opinion: it never merges or deploys anything.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    product: str
    repository: str
    pull_request_number: int = Field(ge=1)
    head_sha: str = ""
    preview_url: str = ""
    decision: ProductReviewDecision
    reason: str = Field(default="", max_length=PRODUCT_REVIEW_REASON_MAX_LENGTH)
    owner_github_id: str
    owner_github_login: str
    decided_at: str
    feedback_url: str = ""
    feedback_requested: bool = False
    # The pull request's base branch when the decision was first shown on it; an
    # acceptance carries only on that base.
    base_branch: str = ""
    carried_from: ProductReviewCarry | None = None

    @model_validator(mode="after")
    def _validate_record(self) -> "ProductReviewDecisionRecord":
        if not self.record_id.strip():
            raise ValueError("product review decision requires record_id")
        if not self.product.strip():
            raise ValueError("product review decision requires product")
        if not self.repository.strip():
            raise ValueError("product review decision requires repository")
        if not self.owner_github_id.isdecimal():
            raise ValueError("product review decision requires a numeric owner_github_id")
        if not self.owner_github_login.strip():
            raise ValueError("product review decision requires owner_github_login")
        if not self.decided_at.strip():
            raise ValueError("product review decision requires decided_at")
        if self.decision == "changes_requested" and not self.reason.strip():
            raise ValueError("product review changes_requested decision requires a reason")
        if self.carried_from is not None and (
            self.decision != "accepted"
            or self.carried_from.head_sha == self.head_sha
            or not self.base_branch.strip()
        ):
            raise ValueError(
                "only an acceptance is carried, only to another head, and only on its base"
            )
        return self
