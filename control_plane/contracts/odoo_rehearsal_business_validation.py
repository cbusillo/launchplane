"""Product-owned scenario receipts; Launchplane interprets no business operations."""

from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane._odoo_rehearsal_business_validation import checked_business_receipt
from control_plane.contracts.canonical_json import canonical_json_sha256


class BusinessRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$")
    id: int = Field(gt=0)


class BusinessCheckEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    passed: bool
    records: list[BusinessRecord]


class BusinessValidationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    product: str = Field(min_length=1)
    rehearsal_id: str = Field(pattern=r"^lp-rehearsal-[a-f0-9]{32}$")
    artifact_id: str = Field(min_length=1)
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    database: str = Field(pattern=r"^lp_rehearsal_[a-f0-9]{32}_final_upgrade$")
    attempt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    passed: bool
    committed: bool
    checks: Annotated[list[BusinessCheckEvidence], Field(min_length=1, max_length=64)]

    @model_validator(mode="after")
    def successful_commit(self) -> Self:
        checked_business_receipt(self.model_dump(mode="json"))
        if self.database != self.rehearsal_id.replace("-", "_") + "_final_upgrade":
            raise ValueError("Validation requires this rehearsal's final copy.")
        return self

    @property
    def sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))
