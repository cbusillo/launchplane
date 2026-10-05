"""Requests to reconcile a product target, raised by GitHub App webhook deliveries."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

ProductReconcileTargetKind = Literal["testing", "preview"]
ProductReconcileRequestState = Literal["pending", "running", "done", "failed"]
PRODUCT_RECONCILE_REQUEST_STATES: tuple[ProductReconcileRequestState, ...] = (
    "pending",
    "running",
    "done",
    "failed",
)


def product_reconcile_target_key(
    *, product: str, target_kind: ProductReconcileTargetKind, pull_request_number: int | None
) -> str:
    if target_kind == "preview":
        return f"{product}:preview:{pull_request_number}"
    return f"{product}:testing"


class ProductReconcileTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product: str = Field(min_length=1)
    target_kind: ProductReconcileTargetKind
    pull_request_number: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_pull_request_number(self) -> "ProductReconcileTarget":
        _validate_target_shape(self.target_kind, self.pull_request_number)
        return self

    @property
    def target_key(self) -> str:
        return product_reconcile_target_key(
            product=self.product,
            target_kind=self.target_kind,
            pull_request_number=self.pull_request_number,
        )


class ProductReconcileRequestRecord(BaseModel):
    """At most one per target; new requests fold into it instead of queueing more."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_key: str = Field(min_length=1)
    product: str = Field(min_length=1)
    target_kind: ProductReconcileTargetKind
    pull_request_number: int | None = Field(default=None, ge=1)
    state: ProductReconcileRequestState
    requested_at: str = Field(min_length=1)
    updated_at: str = Field(min_length=1)
    request_count: int = Field(ge=1)
    rerequested_while_running: bool = False
    last_delivery_id: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    attempt: int = Field(default=0, ge=0)
    last_error: str = ""
    last_plan: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_target(self) -> "ProductReconcileRequestRecord":
        _validate_target_shape(self.target_kind, self.pull_request_number)
        expected_key = product_reconcile_target_key(
            product=self.product,
            target_kind=self.target_kind,
            pull_request_number=self.pull_request_number,
        )
        if self.target_key != expected_key:
            raise ValueError("Product reconcile request target_key does not match its target.")
        return self


class ProductReconcileLeaseLostError(RuntimeError):
    """The caller no longer holds the reconcile request's lease."""


class GitHubAppWebhookDeliveryRecord(BaseModel):
    """A processed GitHub App delivery, kept so a redelivery is recognized."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: str = Field(min_length=1)
    event: str = Field(min_length=1)
    action: str = ""
    repository_id: str = Field(pattern=r"^[1-9][0-9]*$")
    received_at: str = Field(min_length=1)
    target_keys: tuple[str, ...] = ()
    config_authority: dict[str, JsonValue] = Field(default_factory=dict)
    config_authority_request: dict[str, JsonValue] = Field(default_factory=dict)
    config_authority_state: Literal["", "pending", "running", "done", "failed"] = ""
    config_authority_lease_owner: str = ""
    config_authority_lease_expires_at: str = ""
    config_authority_attempt: int = Field(default=0, ge=0)


def _validate_target_shape(
    target_kind: ProductReconcileTargetKind, pull_request_number: int | None
) -> None:
    if target_kind == "preview" and pull_request_number is None:
        raise ValueError("A preview reconcile target requires pull_request_number.")
    if target_kind != "preview" and pull_request_number is not None:
        raise ValueError("Only a preview reconcile target carries pull_request_number.")
