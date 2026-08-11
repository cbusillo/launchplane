from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


ProductRetirementMode = Literal["plan", "apply"]
ProductRetirementOutcome = Literal[
    "planned",
    "started",
    "retired",
    "already_absent",
    "reconcile_required",
    "failed",
]


def canonical_sha256(value: object) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def provider_identifier_sha256(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("Provider identifier digest requires a value.")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


class ProductRetirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    mode: ProductRetirementMode = "plan"
    product: str
    instance: str
    expected_target_sha256: str
    reason: str
    related_issue: str
    reviewed_plan_record_id: str = ""
    reviewed_plan_sha256: str = ""
    confirmation: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "ProductRetirementRequest":
        for field_name in (
            "product",
            "instance",
            "expected_target_sha256",
            "reason",
            "related_issue",
        ):
            value = str(getattr(self, field_name)).strip()
            setattr(self, field_name, value)
            if not value:
                raise ValueError(f"Product retirement requires {field_name}.")
        self.expected_target_sha256 = self.expected_target_sha256.lower()
        self.reviewed_plan_record_id = self.reviewed_plan_record_id.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        self.confirmation = self.confirmation.strip()
        _require_sha256(self.expected_target_sha256, "expected_target_sha256")
        if self.mode == "plan":
            if self.reviewed_plan_record_id or self.reviewed_plan_sha256 or self.confirmation:
                raise ValueError("Product retirement plan rejects apply-only fields.")
            return self
        if not self.reviewed_plan_record_id:
            raise ValueError("Product retirement apply requires reviewed_plan_record_id.")
        _require_sha256(self.reviewed_plan_sha256, "reviewed_plan_sha256")
        if self.confirmation != self.expected_confirmation:
            raise ValueError("Product retirement apply requires exact target-bound confirmation.")
        return self

    @property
    def expected_confirmation(self) -> str:
        return (
            f"retire product {self.product} instance {self.instance} "
            f"target {self.expected_target_sha256}"
        )

    def continuity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "product": self.product,
            "instance": self.instance,
            "expected_target_sha256": self.expected_target_sha256,
            "reason": self.reason,
            "related_issue": self.related_issue,
        }

    @property
    def continuity_sha256(self) -> str:
        return canonical_sha256(self.continuity_payload())


class ProductRetirementIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str
    identity_kind: str
    subject: str = ""
    repository: str = ""
    workflow_ref: str = ""
    environment: str = ""

    @model_validator(mode="after")
    def _validate_identity(self) -> "ProductRetirementIdentity":
        self.actor = self.actor.strip()
        self.identity_kind = self.identity_kind.strip()
        self.subject = self.subject.strip()
        self.repository = self.repository.strip()
        self.workflow_ref = self.workflow_ref.strip()
        self.environment = self.environment.strip()
        if not self.actor or not self.identity_kind:
            raise ValueError("Product retirement identity evidence is incomplete.")
        return self


class ProductRetirementProviderObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: str
    provider_id: Literal["dokploy"] = "dokploy"
    target_type: Literal["application"] = "application"
    target_id: str
    target_id_sha256: str
    state: Literal["present", "absent"]
    application_fingerprint_sha256: str = ""
    application_name_sha256: str = ""
    project_reference_sha256: str = ""
    domain_ids: tuple[str, ...] = ()
    domain_id_sha256: tuple[str, ...] = ()
    domain_host_sha256: tuple[str, ...] = ()
    deployment_status: str = ""
    retirable: bool

    @model_validator(mode="after")
    def _validate_observation(self) -> "ProductRetirementProviderObservation":
        self.observed_at = self.observed_at.strip()
        self.target_id = self.target_id.strip()
        self.deployment_status = self.deployment_status.strip().lower()
        if not self.observed_at or not self.target_id:
            raise ValueError("Product retirement provider observation is incomplete.")
        _require_sha256(self.target_id_sha256, "target_id_sha256")
        if self.target_id_sha256 != provider_identifier_sha256(self.target_id):
            raise ValueError("Product retirement target digest does not match target id.")
        if len(self.domain_ids) != len(self.domain_id_sha256):
            raise ValueError("Product retirement domain identifiers require matching digests.")
        if tuple(provider_identifier_sha256(value) for value in self.domain_ids) != tuple(
            self.domain_id_sha256
        ):
            raise ValueError("Product retirement domain identifier digests do not match.")
        for digest in (
            self.application_fingerprint_sha256,
            self.application_name_sha256,
            self.project_reference_sha256,
            *self.domain_id_sha256,
            *self.domain_host_sha256,
        ):
            if digest:
                _require_sha256(digest, "provider observation digest")
        if self.state == "present" and not all(
            (
                self.application_fingerprint_sha256,
                self.application_name_sha256,
                self.project_reference_sha256,
            )
        ):
            raise ValueError("Present provider observations require complete fingerprints.")
        if self.state == "absent" and self.retirable:
            raise ValueError("Absent provider observations cannot be marked retirable.")
        return self


class ProductRetirementAuthoritySnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    profile_sha256: str
    profile_updated_at: str
    provider_target_sha256: str
    dokploy_target_sha256: str
    dokploy_target_id_sha256: str
    runtime_record_refs: tuple[str, ...] = ()
    runtime_record_sha256: tuple[str, ...] = ()
    secret_record_refs: tuple[str, ...] = ()
    secret_record_sha256: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "ProductRetirementAuthoritySnapshot":
        self.context = self.context.strip()
        self.profile_updated_at = self.profile_updated_at.strip()
        if not self.context or not self.profile_updated_at:
            raise ValueError("Product retirement authority snapshot is incomplete.")
        for digest in (
            self.profile_sha256,
            self.provider_target_sha256,
            self.dokploy_target_sha256,
            self.dokploy_target_id_sha256,
            *self.runtime_record_sha256,
            *self.secret_record_sha256,
        ):
            _require_sha256(digest, "authority snapshot digest")
        if len(self.runtime_record_refs) != len(self.runtime_record_sha256):
            raise ValueError("Runtime retirement references require matching digests.")
        if len(self.secret_record_refs) != len(self.secret_record_sha256):
            raise ValueError("Secret retirement references require matching digests.")
        return self


class ProductRetirementMutationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_operation_key: str = ""
    mutation_reservation_id: str = ""
    reconciliation_key: str = ""
    provider_effect_phases: tuple[str, ...] = ()
    provider_effect_attempted: bool = False
    provider_effect_performed: bool = False
    provider_absence_verified: bool = False
    runtime_delete_event_ids: tuple[str, ...] = ()
    deleted_authority_refs: tuple[str, ...] = ()
    disabled_secret_record_sha256: tuple[str, ...] = ()
    secret_disable_event_sha256: tuple[str, ...] = ()
    lifecycle_before: Literal["", "active", "retiring", "retired"] = ""
    lifecycle_after: Literal["", "active", "retiring", "retired"] = ""
    error_code: str = ""
    error_message: str = Field(default="", max_length=1000)


class ProductRetirementRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    record_id: str
    plan_record_id: str
    mode: ProductRetirementMode
    outcome: ProductRetirementOutcome
    product: str
    context: str
    instance: str
    identity: ProductRetirementIdentity
    reason: str
    related_issue: str
    idempotency_key: str
    trace_id: str
    requested_at: str
    recorded_at: str
    completed_at: str = ""
    continuity_sha256: str
    plan_sha256: str
    reviewed_plan_record_id: str = ""
    reviewed_plan_sha256: str = ""
    provider_observation: ProductRetirementProviderObservation
    authority_snapshot: ProductRetirementAuthoritySnapshot
    mutation_evidence: ProductRetirementMutationEvidence = Field(
        default_factory=ProductRetirementMutationEvidence
    )

    @model_validator(mode="after")
    def _validate_record(self) -> "ProductRetirementRecord":
        for field_name in (
            "record_id",
            "plan_record_id",
            "product",
            "context",
            "instance",
            "reason",
            "related_issue",
            "idempotency_key",
            "trace_id",
            "requested_at",
            "recorded_at",
        ):
            value = str(getattr(self, field_name)).strip()
            setattr(self, field_name, value)
            if not value:
                raise ValueError(f"Product retirement record requires {field_name}.")
        for digest in (
            self.continuity_sha256,
            self.plan_sha256,
        ):
            _require_sha256(digest, "product retirement digest")
        if self.mode == "plan":
            if self.outcome != "planned" or self.record_id != self.plan_record_id:
                raise ValueError("Product retirement plans require one planned plan record.")
            if self.reviewed_plan_record_id or self.reviewed_plan_sha256:
                raise ValueError("Product retirement plans reject reviewed-plan fields.")
        else:
            if self.outcome == "planned":
                raise ValueError("Product retirement apply records require apply outcomes.")
            if not self.reviewed_plan_record_id:
                raise ValueError("Product retirement apply records require reviewed plan identity.")
            _require_sha256(self.reviewed_plan_sha256, "reviewed_plan_sha256")
            if self.reviewed_plan_record_id != self.plan_record_id:
                raise ValueError("Product retirement apply must bind its plan record.")
            if self.reviewed_plan_sha256 != self.plan_sha256:
                raise ValueError("Product retirement apply must bind its plan digest.")
        return self


def build_product_retirement_record_id(*, trace_id: str, outcome: str) -> str:
    normalized_trace_id = trace_id.strip()
    normalized_outcome = outcome.strip()
    if not normalized_trace_id or not normalized_outcome:
        raise ValueError("Product retirement record ids require trace and outcome.")
    return f"product-retirement-{normalized_trace_id}-{normalized_outcome}"


def _require_sha256(value: str, field_name: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value.strip().lower()):
        raise ValueError(f"Product retirement {field_name} must be SHA-256.")
