from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.merge_train_policy import (
    ProviderDeliveryProtectionExpectationV1,
)


ProviderDeliveryInspectionStatus = Literal[
    "ready",
    "protection_not_ready",
    "semantic_inconclusive",
]
ProviderDeliveryInspectionReason = Literal[
    "provider_protection_ready",
    "repository_identity_mismatch",
    "branch_identity_mismatch",
    "branch_not_protected",
    "provider_response_malformed",
    "provider_visibility_incomplete",
    "ruleset_page_full",
    "ruleset_limit_exceeded",
    "inherited_ruleset_visibility_unavailable",
    "unknown_ruleset_source",
    "unknown_rule_type",
    "unknown_bypass_actor",
    "unknown_bypass_mode",
    "classic_response_incomplete",
    "unsupported_protection_shape",
    "update_ruleset_missing",
    "multiple_update_rulesets",
    "update_ruleset_not_isolated",
    "update_bypass_not_exclusive",
    "gate_ruleset_bypass_present",
    "deletion_not_protected",
    "non_fast_forward_not_protected",
    "required_status_checks_mismatch",
    "strict_status_checks_mismatch",
    "code_scanning_mismatch",
    "pull_request_mismatch",
    "allowed_merge_methods_mismatch",
]
ProviderDeliveryInspectionRulesetId = Annotated[
    int,
    Field(strict=True, gt=0, le=2**63 - 1),
]


class ProviderDeliveryInspectionFactsV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    repository_id: int = Field(strict=True, gt=0, le=2**63 - 1)
    repository_owner_id: int = Field(strict=True, gt=0, le=2**63 - 1)
    repository: str = Field(min_length=3, max_length=512)
    base_branch: str = Field(min_length=1, max_length=512)
    ordinary_delivery_app_id: int = Field(strict=True, gt=0, le=2**63 - 1)
    applicable_ruleset_ids: tuple[ProviderDeliveryInspectionRulesetId, ...] = Field(max_length=20)
    update_ruleset_id: int = Field(strict=True, gt=0, le=2**63 - 1)
    classic_protection_present: bool
    effective_protection: ProviderDeliveryProtectionExpectationV1
    raw_observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_request_count: int = Field(strict=True, ge=1, le=32)

    @field_validator("repository", "base_branch")
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("provider inspection target values must be non-empty")
        return normalized

    @field_validator("applicable_ruleset_ids")
    @classmethod
    def _normalize_ruleset_ids(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(isinstance(item, bool) or item < 1 or item > 2**63 - 1 for item in value):
            raise ValueError("provider inspection ruleset ids must be positive integers")
        ordered = tuple(sorted(value))
        if len(ordered) != len(set(ordered)):
            raise ValueError("provider inspection ruleset ids must be unique")
        return ordered

    @model_validator(mode="after")
    def _validate_facts(self) -> "ProviderDeliveryInspectionFactsV1":
        owner, separator, name = self.repository.partition("/")
        if (
            not separator
            or not owner
            or not name
            or "/" in name
            or self.update_ruleset_id not in set(self.applicable_ruleset_ids)
        ):
            raise ValueError("provider inspection facts require an exact update ruleset")
        return self


class ProviderDeliveryInspectionResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    status: ProviderDeliveryInspectionStatus
    reason_codes: tuple[ProviderDeliveryInspectionReason, ...] = Field(
        min_length=1,
        max_length=16,
    )
    facts: ProviderDeliveryInspectionFactsV1 | None = None
    raw_observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_request_count: int = Field(strict=True, ge=1, le=32)

    @field_validator("reason_codes")
    @classmethod
    def _normalize_reasons(
        cls,
        value: tuple[ProviderDeliveryInspectionReason, ...],
    ) -> tuple[ProviderDeliveryInspectionReason, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def _validate_result(self) -> "ProviderDeliveryInspectionResultV1":
        if self.status == "ready":
            if self.reason_codes != ("provider_protection_ready",) or self.facts is None:
                raise ValueError("ready provider inspection requires exact ready facts")
        elif "provider_protection_ready" in self.reason_codes:
            raise ValueError("non-ready provider inspection cannot carry the ready reason")
        if self.facts is not None and (
            self.facts.raw_observation_sha256 != self.raw_observation_sha256
            or self.facts.provider_request_count != self.provider_request_count
        ):
            raise ValueError("provider inspection result and facts must share provenance")
        return self
