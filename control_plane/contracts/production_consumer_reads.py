from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReviewedOdooConsumer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product: str = Field(min_length=1, max_length=128)
    environment: str = Field(default="", max_length=128)
    preview_id: str = Field(default="", max_length=256)

    @model_validator(mode="after")
    def require_one_runtime(self) -> Self:
        if bool(self.environment) == bool(self.preview_id):
            raise ValueError("Select one stable environment or reviewed preview.")
        return self


class ProductionConsumerReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumers: tuple[ReviewedOdooConsumer, ...] = Field(min_length=1, max_length=16)
    coordination_reference: str = Field(min_length=1, max_length=512)

    @model_validator(mode="after")
    def unique_members(self) -> Self:
        if len(set(self.consumers)) != len(self.consumers):
            raise ValueError("Reviewed consumers must be unique.")
        return self


ReadStatus = Literal["verified", "failed", "unknown", "unsupported"]


class ConsumerReadEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    consumer: ReviewedOdooConsumer
    status: ReadStatus = "unknown"
    query: ReadStatus = "unknown"
    populated: ReadStatus = "unknown"
    destination: ReadStatus = "unknown"
    evidence_reference: str = ""


class ProductionConsumerReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: ReadStatus = "unknown"
    observed_at: str
    completed_at: str
    publication_sha256: str = ""
    source_observation_reference: str = ""
    consumers: tuple[ConsumerReadEvidence, ...] = Field(default=(), max_length=16)
    values_redacted: Literal[True] = True
    applied: Literal[False] = False
