from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_review import ProductReviewIntegratedChange
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256

GitSha = Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
EvidenceDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]

INTEGRATION_ATTESTATION_ROUTE = "/v1/product-review/integration-attestation"
INTEGRATION_ATTESTATION_ACTION = "product_review.integration_attestation"


class IntegrationAttestationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product: str
    repository: str
    pull_request_number: int = Field(gt=0)
    base_branch: str
    owner_github_id: str
    source_decision_id: str
    merge_sha: GitSha
    tree_sha: GitSha
    parent_shas: tuple[GitSha, GitSha]
    integrated_changes: tuple[ProductReviewIntegratedChange, ...]
    batch_record_ids: tuple[str, ...] = ()

    def digest(self) -> str:
        return nonvalidating_compact_sorted_json_sha256(self.model_dump(mode="json"))


class IntegrationModelReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    evidence_sha256: EvidenceDigest
    author_provider: Literal["openai", "anthropic", "google"]
    reviewer_provider: Literal["openai", "anthropic", "google"]
    reviewer_model: str = Field(min_length=1, max_length=128)
    conclusion: Literal["no_new_client_visible_work"]
    report: str = Field(min_length=1, max_length=64000)

    @model_validator(mode="after")
    def independent_review(self) -> "IntegrationModelReview":
        if self.author_provider == self.reviewer_provider:
            raise ValueError("Integration attestation requires a review from another provider.")
        return self


class IntegrationAttestationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence: IntegrationAttestationEvidence
    review: IntegrationModelReview
    statement: str = Field(min_length=1, max_length=4000)
    recorded_by: str = Field(min_length=1)
    recorded_at: str = Field(min_length=1)

    @model_validator(mode="after")
    def reviewed_evidence_matches(self) -> "IntegrationAttestationRecord":
        if self.review.evidence_sha256 != self.evidence.digest():
            raise ValueError("The model review must confirm this exact integration evidence.")
        if not self.statement.strip():
            raise ValueError("The engineering statement cannot be blank.")
        return self

    @property
    def record_id(self) -> str:
        return "integration-attestation-" + self.evidence.digest()


class IntegrationAttestationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    product: str = Field(min_length=1, max_length=128)
    repository: str = Field(pattern=r"^[^/\s]+/[^/\s]+$")
    pull_request_number: int = Field(gt=0)
    expected_head_sha: GitSha
    merge_sha: GitSha
    source_decision_id: str = Field(min_length=1, max_length=512)
    reviewed_evidence_sha256: str = ""
    statement: str = Field(default="", max_length=4000)
    review: IntegrationModelReview | None = None


class IntegrationAttestationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    evidence: IntegrationAttestationEvidence
    evidence_sha256: EvidenceDigest
    record: IntegrationAttestationRecord | None = None
