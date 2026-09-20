from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ProductReviewDecision = Literal["accepted", "changes_requested"]
PRODUCT_REVIEW_REASON_MAX_LENGTH = 4000


class ProductReviewDecisionRecord(BaseModel):
    """One Owner decision about one pull request preview.

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
        return self
