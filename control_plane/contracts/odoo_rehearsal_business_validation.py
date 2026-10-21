"""Bounded Odoo ORM batches, supplied by the product and limited to synthetic records."""

from typing import Annotated, Literal, Self
import hashlib
import json

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256


BusinessModel = Literal[
    "res.partner",
    "product.product",
    "product.template",
    "sale.order",
    "sale.order.line",
    "stock.location",
    "stock.quant",
    "stock.picking",
    "stock.move",
    "stock.move.line",
]
BusinessKey = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")]
BUSINESS_METHODS = {
    "sale.order": {"action_confirm"},
    "stock.quant": {"action_apply_inventory"},
    "stock.picking": {"action_assign", "button_validate"},
}


class BusinessReference(BaseModel):
    model_config = ConfigDict(extra="forbid", serialize_by_alias=True)
    ref: BusinessKey = Field(alias="$ref")


class BusinessCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["create"]
    key: BusinessKey
    model: BusinessModel
    values: dict[str, str | int | float | bool | None | BusinessReference] = Field(
        min_length=1, max_length=40
    )


class BusinessCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["call"]
    key: BusinessKey
    record: BusinessKey
    method: Literal["action_confirm", "action_apply_inventory", "action_assign", "button_validate"]


class BusinessAssert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    op: Literal["assert"]
    key: BusinessKey
    record: BusinessKey
    field: str = Field(pattern=r"^[a-z][a-z0-9_]{0,79}$")
    comparison: Literal["equals", "truthy"] = "equals"
    expected: JsonValue


BusinessStep = Annotated[BusinessCreate | BusinessCall | BusinessAssert, Field(discriminator="op")]


class BusinessValidation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    database: str = Field(pattern=r"^lp_rehearsal_[a-f0-9]{32}_final_upgrade$")
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    steps: list[BusinessStep] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def synthetic_references(self) -> Self:
        models: dict[str, str] = {}
        keys: set[str] = set()
        assertions = 0

        for step in self.steps:
            if step.key in keys:
                raise ValueError("Step keys must be unique.")
            if isinstance(step, BusinessCreate):
                for value in step.values.values():
                    if isinstance(value, BusinessReference) and value.ref not in models:
                        raise ValueError("References must name an earlier synthetic create.")
                models[step.key] = step.model
            else:
                if step.record not in models:
                    raise ValueError("Operations may target only earlier synthetic records.")
                if isinstance(step, BusinessCall):
                    if step.method not in BUSINESS_METHODS.get(models[step.record], set()):
                        raise ValueError("This business method is unavailable on the model.")
                else:
                    assertions += 1
            keys.add(step.key)
        encoded = json.dumps(self.model_dump(mode="json"), allow_nan=False)
        if not models or not assertions or len(encoded.encode()) > 65536:
            raise ValueError("A bounded batch requires synthetic creates and assertions.")
        return self

    @property
    def sha256(self) -> str:
        # Business quantities allow decimals; the shared canonical encoder intentionally rejects floats.
        return hashlib.sha256(
            json.dumps(
                self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()


class BusinessStepEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    key: BusinessKey
    # No database values, exception messages, command output or credentials.
    record_ids: list[Annotated[int, Field(gt=0)]] = Field(max_length=100)
    passed: bool


class BusinessValidationReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")
    validation_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    database: str = Field(pattern=r"^lp_rehearsal_[a-f0-9]{32}_final_upgrade$")
    source_commit: str = Field(pattern=r"^[a-f0-9]{40}$")
    steps: list[BusinessStepEvidence] = Field(min_length=1, max_length=64)
    passed: bool
    committed: bool

    @model_validator(mode="after")
    def successful_commit(self) -> Self:
        if self.passed and not self.committed:
            raise ValueError("A passing batch requires confirmed committed writes.")
        return self

    @property
    def sha256(self) -> str:
        return canonical_json_sha256(self.model_dump(mode="json"))
