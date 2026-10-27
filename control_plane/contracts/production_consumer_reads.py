from enum import StrEnum
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


class ConsumerTableRole(StrEnum):
    CUSTOMER = "CUSTOMER_TABLE"
    CONTACT = "CONTACT_TABLE"
    PRODUCT = "PRODUCT_TABLE"
    TICKET = "TICKET_TABLE"
    TICKET_PROPERTIES = "TICKET_PROPERTIES_TABLE"
    TICKET_COMMENT = "TICKET_COMMENT_TABLE"
    ESTIMATE = "ESTIMATE_TABLE"
    ESTIMATE_LINE = "ESTIMATE_LINE_ITEM_TABLE"
    INVOICE = "INVOICE_TABLE"
    INVOICE_LINE = "INVOICE_LINE_ITEM_TABLE"


class ProductionConsumerReview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consumers: tuple[ReviewedOdooConsumer, ...] = Field(min_length=1, max_length=16)
    coordination_reference: str = Field(min_length=1, max_length=512)
    required_populated_roles: tuple[ConsumerTableRole, ...] = Field(
        default=(), max_length=len(ConsumerTableRole)
    )

    @model_validator(mode="after")
    def unique_members(self) -> Self:
        if len(set(self.consumers)) != len(self.consumers):
            raise ValueError("Reviewed consumers must be unique.")
        if len(set(self.required_populated_roles)) != len(self.required_populated_roles):
            raise ValueError("Reviewed population roles must be unique.")
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
    busy: bool = Field(default=False, exclude_if=lambda value: not value)
    values_redacted: Literal[True] = True
    applied: Literal[False] = False
