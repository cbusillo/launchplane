"""Private persisted contracts for demand-triggered provider readiness."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_train_policy import ProviderDeliveryProtectionExpectationV1
from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget, StrictFrozenModel
from control_plane.contracts.provider_delivery_inspection import (
    ProviderDeliveryInspectionFactsV1,
    ProviderDeliveryInspectionResultV1,
)


PROVIDER_READINESS_FRESHNESS_SECONDS = 300
PROVIDER_READINESS_REQUIRED_MARGIN_SECONDS = 30
PROVIDER_INSPECTION_TOTAL_SECONDS = 45
PROVIDER_INSPECTION_CLEANUP_SECONDS = 10
PROVIDER_INSPECTION_PUBLICATION_ELIGIBILITY_SECONDS = 60
PROVIDER_INSPECTION_MAX_ATTEMPTS = 3
PROVIDER_INSPECTION_MAX_RETRY_AFTER_SECONDS = 3705

ProviderDeliveryInspectionPhase = Literal["active", "terminal"]
ProviderDeliveryInspectionCustodyPhase = Literal[
    "reserved",
    "minting",
    "issued",
    "issue_unknown",
    "cleanup_unknown",
    "closed",
]
ProviderDeliveryObservationClass = Literal["protection_conclusive", "capability_unavailable"]
ProviderDeliveryObservationStatus = Literal[
    "ready",
    "protection_not_ready",
    "semantic_inconclusive",
    "capability_unavailable",
]
ProviderDeliveryReadinessReason = Literal[
    "provider_protection_ready",
    "provider_readiness_refresh_required",
    "provider_readiness_in_progress",
    "provider_readiness_admission_replay_required",
    "provider_wait",
    "provider_inspection_deadline",
    "provider_inspection_attempts_exhausted",
    "provider_inspection_custody_fenced",
    "provider_inspection_permission_denied",
    "provider_inspection_profile_unavailable",
    "protection_expectation_unavailable",
    "provider_protection_not_ready",
    "provider_protection_inconclusive",
    "provider_inspection_abandoned",
    "provider_inspection_late_result",
]


class ProviderDeliveryInspectionBindingV1(StrictFrozenModel):
    """Current semantic binding and historical authority provenance."""

    target: OrdinaryAgentTarget
    repository_owner_id: int = Field(gt=0, le=2**63 - 1)
    inventory_record_id: str = Field(min_length=1, max_length=256)
    inventory_revision: int = Field(ge=1, le=2**63 - 1)
    inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    installed_activation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    ordinary_delivery_app_id: int = Field(gt=0, le=2**63 - 1)
    ordinary_delivery_installation_id: int = Field(gt=0, le=2**63 - 1)
    merge_policy_record_id: str = Field(min_length=1, max_length=256)
    merge_policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    merge_policy_semantics_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expectation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspection_profile_id: str = Field(min_length=1, max_length=256)
    inspection_profile_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    inspection_app_id: int = Field(gt=0, le=2**63 - 1)
    inspection_secret_id: str = Field(min_length=1, max_length=256)
    inspection_secret_binding_id: str = Field(min_length=1, max_length=256)
    inspection_secret_version_id: str = Field(min_length=1, max_length=256)
    permission_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_independent_app(self) -> Self:
        if self.inspection_app_id == self.ordinary_delivery_app_id:
            raise ValueError("provider inspection must use an independent GitHub App")
        return self


class ProviderDeliveryInspectionAttemptV1(StrictFrozenModel):
    schema_version: Literal[1] = 1
    attempt_id: str = Field(pattern=r"^provider-inspection-[0-9a-f]{64}$")
    demand_id: str = Field(min_length=1, max_length=256)
    client_intent_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    session_id: str = Field(min_length=1, max_length=256)
    lease_id: str = Field(min_length=1, max_length=256)
    generation: int = Field(ge=1, le=2**63 - 1)
    provider_attempt_ordinal: int = Field(ge=1, le=PROVIDER_INSPECTION_MAX_ATTEMPTS)
    action_ordinal: int = Field(ge=1, le=2**63 - 1)
    binding: ProviderDeliveryInspectionBindingV1
    revision: int = Field(default=1, ge=1, le=2**63 - 1)
    inspection_phase: ProviderDeliveryInspectionPhase = "active"
    custody_phase: ProviderDeliveryInspectionCustodyPhase = "reserved"
    inspection_started_at: int = Field(ge=0, le=2**63 - 1)
    observation_anchor: int = Field(ge=0, le=2**63 - 1)
    dispatch_deadline: int = Field(ge=1, le=2**63 - 1)
    publication_deadline: int = Field(ge=1, le=2**63 - 1)
    token_expires_at: int | None = Field(default=None, ge=1, le=2**63 - 1)
    inspection_installation_id: int | None = Field(default=None, gt=0, le=2**63 - 1)
    next_retry_not_before: int | None = Field(default=None, ge=1, le=2**63 - 1)
    terminal_class: ProviderDeliveryObservationClass | None = None
    terminal_status: ProviderDeliveryObservationStatus | None = None
    reason_codes: tuple[ProviderDeliveryReadinessReason, ...] = ()
    repository_completion_sequence: int | None = Field(default=None, ge=1, le=2**63 - 1)
    receipt_id: str | None = Field(default=None, max_length=256)
    facts: ProviderDeliveryInspectionFactsV1 | None = None
    inspection_result: ProviderDeliveryInspectionResultV1 | None = None
    provider_request_count: int = Field(default=0, ge=0, le=256)
    completed_at: int | None = Field(default=None, ge=0, le=2**63 - 1)

    @field_validator("reason_codes", mode="before")
    @classmethod
    def _read_reason_codes(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_state(self) -> Self:
        if self.dispatch_deadline != self.inspection_started_at + PROVIDER_INSPECTION_TOTAL_SECONDS:
            raise ValueError("provider inspection dispatch deadline is not canonical")
        if (
            self.publication_deadline
            != self.inspection_started_at + PROVIDER_INSPECTION_PUBLICATION_ELIGIBILITY_SECONDS
        ):
            raise ValueError("provider inspection publication deadline is not canonical")
        terminal_values = (
            self.terminal_class,
            self.terminal_status,
            self.repository_completion_sequence,
            self.completed_at,
        )
        if self.inspection_phase == "terminal":
            if any(value is None for value in terminal_values):
                raise ValueError("terminal provider inspection requires complete terminal evidence")
            if self.custody_phase != "closed":
                raise ValueError("terminal provider inspection must have closed custody")
        elif any(value is not None for value in terminal_values) or self.receipt_id is not None:
            raise ValueError("active provider inspection cannot claim terminal evidence")
        if self.terminal_status == "ready" and (
            self.facts is None or self.receipt_id is None or self.inspection_installation_id is None
        ):
            raise ValueError("ready provider inspection requires facts and receipt")
        if self.terminal_status != "ready" and self.receipt_id is not None:
            raise ValueError("only ready provider inspection may publish a receipt")
        if self.inspection_result is not None and self.inspection_result.facts != self.facts:
            raise ValueError("provider inspection terminal result and facts disagree")
        if self.facts is not None:
            facts_match = provider_delivery_facts_match_binding(self.facts, self.binding)
            if self.terminal_status == "ready":
                facts_match = facts_match and provider_delivery_ready_facts_match_binding(
                    self.facts, self.binding
                )
            if not facts_match:
                raise ValueError("provider inspection facts do not match the reserved binding")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("provider inspection reasons must be unique")
        return self


class ProviderDeliveryReadinessReceiptV1(StrictFrozenModel):
    schema_version: Literal[1] = 1
    receipt_id: str = Field(pattern=r"^provider-readiness-[0-9a-f]{64}$")
    attempt_id: str = Field(pattern=r"^provider-inspection-[0-9a-f]{64}$")
    demand_id: str = Field(min_length=1, max_length=256)
    generation: int = Field(ge=1, le=2**63 - 1)
    action_ordinal: int = Field(ge=1, le=2**63 - 1)
    inspection_installation_id: int = Field(gt=0, le=2**63 - 1)
    binding: ProviderDeliveryInspectionBindingV1
    facts: ProviderDeliveryInspectionFactsV1
    reason_codes: tuple[ProviderDeliveryReadinessReason, ...] = ()
    provider_request_count: int = Field(ge=0, le=256)
    repository_completion_sequence: int = Field(ge=1, le=2**63 - 1)
    observed_at: int = Field(ge=0, le=2**63 - 1)
    expires_at: int = Field(ge=1, le=2**63 - 1)
    completed_at: int = Field(ge=0, le=2**63 - 1)
    receipt_sha256: str = ""

    @field_validator("reason_codes", mode="before")
    @classmethod
    def _read_reason_codes(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def _validate_receipt(self) -> Self:
        if self.expires_at != self.observed_at + PROVIDER_READINESS_FRESHNESS_SECONDS:
            raise ValueError("provider readiness expiry is not anchored to observation")
        if self.completed_at < self.observed_at:
            raise ValueError("provider readiness completion predates observation")
        if not provider_delivery_ready_facts_match_binding(self.facts, self.binding):
            raise ValueError("provider readiness facts do not match the receipt binding")
        digest = canonical_json_sha256(self.model_dump(mode="json", exclude={"receipt_sha256"}))
        if self.receipt_sha256 and self.receipt_sha256 != digest:
            raise ValueError("provider readiness receipt digest mismatch")
        object.__setattr__(self, "receipt_sha256", digest)
        return self


class ProviderDeliveryReadinessDecision(StrictFrozenModel):
    status: Literal[
        "ready",
        "refresh_required",
        "in_progress",
        "protection_not_ready",
        "semantic_inconclusive",
        "capability_unavailable",
    ]
    reason_code: ProviderDeliveryReadinessReason
    server_observed_at: int = Field(ge=0, le=2**63 - 1)
    retry_not_before: int | None = Field(default=None, ge=1, le=2**63 - 1)
    receipt: ProviderDeliveryReadinessReceiptV1 | None = None

    @model_validator(mode="after")
    def _validate_decision(self) -> Self:
        if (self.status == "ready") != (self.receipt is not None):
            raise ValueError("only ready provider decisions carry a receipt")
        if self.status != "ready" and self.retry_not_before is None:
            raise ValueError("non-ready provider decisions require retry pacing")
        return self


class ProviderDeliveryInspectionReservationV1(StrictFrozenModel):
    attempt: ProviderDeliveryInspectionAttemptV1
    expectation: ProviderDeliveryProtectionExpectationV1


def provider_delivery_inspection_attempt_id(
    *, demand_id: str, generation: int, provider_attempt_ordinal: int
) -> str:
    return "provider-inspection-" + canonical_json_sha256(
        {
            "domain": "provider-delivery-inspection-attempt-v1",
            "demand_id": demand_id,
            "generation": generation,
            "provider_attempt_ordinal": provider_attempt_ordinal,
        }
    )


def provider_delivery_readiness_receipt_id(*, attempt_id: str) -> str:
    return "provider-readiness-" + canonical_json_sha256(
        {"domain": "provider-delivery-readiness-receipt-v1", "attempt_id": attempt_id}
    )


def provider_delivery_binding_is_current(
    recorded: ProviderDeliveryInspectionBindingV1,
    current: ProviderDeliveryInspectionBindingV1,
) -> bool:
    """Compare consumed semantics while retaining containing-record provenance."""
    excluded = {"merge_policy_record_id", "merge_policy_sha256"}
    return recorded.model_dump(mode="json", exclude=excluded) == current.model_dump(
        mode="json", exclude=excluded
    )


def provider_delivery_facts_match_binding(
    facts: ProviderDeliveryInspectionFactsV1,
    binding: ProviderDeliveryInspectionBindingV1,
) -> bool:
    return (
        facts.repository_id == binding.target.repository_id
        and facts.repository_owner_id == binding.repository_owner_id
        and facts.repository == binding.target.repository
        and facts.base_branch == binding.target.base_branch
        and facts.ordinary_delivery_app_id == binding.ordinary_delivery_app_id
    )


def provider_delivery_ready_facts_match_binding(
    facts: ProviderDeliveryInspectionFactsV1,
    binding: ProviderDeliveryInspectionBindingV1,
) -> bool:
    return (
        provider_delivery_facts_match_binding(facts, binding)
        and canonical_json_sha256(facts.effective_protection.model_dump(mode="json"))
        == binding.expectation_sha256
    )
