from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_retirement import canonical_sha256, provider_identifier_sha256


DetachedApplicationRetirementMode = Literal["plan", "apply"]
DetachedApplicationRetirementOutcome = Literal[
    "planned",
    "retired",
    "already_absent",
    "reconcile_required",
    "failed",
]
DeploymentHistoryState = Literal["present", "no_history"]
MAX_DETACHED_APPLICATION_RETIREMENT_ERROR_MESSAGE_LENGTH = 1000


class DetachedApplicationRetirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    mode: DetachedApplicationRetirementMode = "plan"
    project_name: str
    environment_name: str
    application_name: str
    candidate_target_sha256: str
    expected_protected_target_sha256: tuple[str, ...]
    reason: str
    related_issue: str
    reviewed_plan_record_id: str = ""
    reviewed_plan_sha256: str = ""
    confirmation: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "DetachedApplicationRetirementRequest":
        for field_name in (
            "project_name",
            "environment_name",
            "application_name",
            "candidate_target_sha256",
            "reason",
            "related_issue",
        ):
            value = str(getattr(self, field_name)).strip()
            setattr(self, field_name, value)
            if not value:
                raise ValueError(f"Detached application retirement requires {field_name}.")
        self.candidate_target_sha256 = self.candidate_target_sha256.lower()
        _require_sha256(self.candidate_target_sha256, "candidate_target_sha256")
        protected = tuple(value.strip().lower() for value in self.expected_protected_target_sha256)
        if not protected or any(not value for value in protected):
            raise ValueError("Detached application retirement requires protected target digests.")
        for digest in protected:
            _require_sha256(digest, "expected_protected_target_sha256")
        if protected != tuple(sorted(set(protected))):
            raise ValueError(
                "Detached application retirement protected target digests must be unique and sorted."
            )
        if self.candidate_target_sha256 in protected:
            raise ValueError(
                "Detached application retirement candidate cannot be a protected target."
            )
        self.expected_protected_target_sha256 = protected
        self.reviewed_plan_record_id = self.reviewed_plan_record_id.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        self.confirmation = self.confirmation.strip()
        if self.mode == "plan":
            if self.reviewed_plan_record_id or self.reviewed_plan_sha256 or self.confirmation:
                raise ValueError("Detached application retirement plan rejects apply-only fields.")
            return self
        if not self.reviewed_plan_record_id:
            raise ValueError(
                "Detached application retirement apply requires reviewed_plan_record_id."
            )
        _require_sha256(self.reviewed_plan_sha256, "reviewed_plan_sha256")
        if self.confirmation != self.expected_confirmation:
            raise ValueError(
                "Detached application retirement apply requires exact target-bound confirmation."
            )
        return self

    @property
    def expected_confirmation(self) -> str:
        return (
            "retire detached Dokploy application "
            f"{self.project_name}/{self.environment_name}/{self.application_name} "
            f"target {self.candidate_target_sha256}"
        )

    def continuity_payload(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "project_name": self.project_name,
            "environment_name": self.environment_name,
            "application_name": self.application_name,
            "candidate_target_sha256": self.candidate_target_sha256,
            "expected_protected_target_sha256": self.expected_protected_target_sha256,
            "reason": self.reason,
            "related_issue": self.related_issue,
        }

    @property
    def continuity_sha256(self) -> str:
        return canonical_sha256(self.continuity_payload())


class DetachedApplicationRetirementIdentity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str
    identity_kind: str
    subject: str = ""
    repository: str = ""
    workflow_ref: str = ""
    environment: str = ""

    @model_validator(mode="after")
    def _validate_identity(self) -> "DetachedApplicationRetirementIdentity":
        for field_name in (
            "actor",
            "identity_kind",
            "subject",
            "repository",
            "workflow_ref",
            "environment",
        ):
            setattr(self, field_name, str(getattr(self, field_name)).strip())
        if not self.actor or not self.identity_kind:
            raise ValueError("Detached application retirement identity is incomplete.")
        return self


class DetachedApplicationProviderObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observed_at: str
    provider_id: Literal["dokploy"] = "dokploy"
    target_type: Literal["application"] = "application"
    project_id: str
    project_name: str
    environment_id: str
    environment_name: str
    application_id: str
    application_name: str
    target_id_sha256: str
    state: Literal["present", "absent"]
    application_state: str = ""
    application_fingerprint_sha256: str = ""
    domain_ids: tuple[str, ...] = ()
    domain_id_sha256: tuple[str, ...] = ()
    domain_host_sha256: tuple[str, ...] = ()
    deployment_history_state: DeploymentHistoryState | Literal[""] = ""
    latest_deployment_sha256: str = ""
    safe_to_retire: bool = False

    @model_validator(mode="after")
    def _validate_observation(self) -> "DetachedApplicationProviderObservation":
        for field_name in (
            "observed_at",
            "project_id",
            "project_name",
            "environment_id",
            "environment_name",
            "application_id",
            "application_name",
            "application_state",
            "application_fingerprint_sha256",
            "deployment_history_state",
            "latest_deployment_sha256",
        ):
            setattr(self, field_name, str(getattr(self, field_name)).strip())
        if not self.observed_at or not self.application_id:
            raise ValueError("Detached application provider observation is incomplete.")
        _require_sha256(self.target_id_sha256, "target_id_sha256")
        if self.target_id_sha256 != provider_identifier_sha256(self.application_id):
            raise ValueError("Detached application target digest does not match target id.")
        if len(self.domain_ids) != len(self.domain_id_sha256):
            raise ValueError("Detached application domain identifiers require matching digests.")
        if tuple(provider_identifier_sha256(value) for value in self.domain_ids) != tuple(
            self.domain_id_sha256
        ):
            raise ValueError("Detached application domain identifier digests do not match.")
        for digest in (
            self.application_fingerprint_sha256,
            *self.domain_id_sha256,
            *self.domain_host_sha256,
        ):
            if digest:
                _require_sha256(digest, "provider observation digest")
        if self.latest_deployment_sha256:
            _require_sha256(self.latest_deployment_sha256, "latest_deployment_sha256")
        if self.state == "present":
            if not all(
                (
                    self.project_id,
                    self.project_name,
                    self.environment_id,
                    self.environment_name,
                    self.application_name,
                    self.application_state,
                    self.application_fingerprint_sha256,
                    self.deployment_history_state,
                )
            ):
                raise ValueError(
                    "Present detached application observations require complete evidence."
                )
        elif self.safe_to_retire:
            raise ValueError("Absent detached applications cannot be marked safe to retire.")
        if self.safe_to_retire and not (
            self.state == "present"
            and self.application_state == "idle"
            and self.deployment_history_state == "no_history"
            and not self.latest_deployment_sha256
            and not self.domain_ids
        ):
            raise ValueError("Detached application safe-to-retire evidence is inconsistent.")
        return self


class DetachedApplicationProtectedTargetSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    application_id: str
    target_id_sha256: str
    application_name: str
    application_fingerprint_sha256: str
    domain_count: int = Field(ge=0)
    domains_sha256: str
    deployment_history_state: DeploymentHistoryState
    deployment_history_sha256: str

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "DetachedApplicationProtectedTargetSnapshot":
        self.application_id = self.application_id.strip()
        self.application_name = self.application_name.strip()
        if not self.application_id or not self.application_name:
            raise ValueError("Protected detached application snapshot is incomplete.")
        if self.target_id_sha256 != provider_identifier_sha256(self.application_id):
            raise ValueError("Protected detached application target digest does not match id.")
        for digest in (
            self.target_id_sha256,
            self.application_fingerprint_sha256,
            self.domains_sha256,
            self.deployment_history_sha256,
        ):
            _require_sha256(digest, "protected target digest")
        return self


class DetachedApplicationAuthorityAbsenceSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    record_count: int = Field(ge=0)
    records_sha256: str
    match_count: Literal[0] = 0

    @model_validator(mode="after")
    def _validate_source(self) -> "DetachedApplicationAuthorityAbsenceSource":
        self.source = self.source.strip()
        if not self.source:
            raise ValueError("Authority absence source requires a source name.")
        _require_sha256(self.records_sha256, "authority source digest")
        return self


class DetachedApplicationAuthorityAbsenceProof(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_target_sha256: str
    candidate_application_name_sha256: str
    source_count: int = Field(ge=1)
    record_count: int = Field(ge=0)
    sources: tuple[DetachedApplicationAuthorityAbsenceSource, ...]
    match_count: Literal[0] = 0
    authority_write_count: Literal[0] = 0

    @model_validator(mode="after")
    def _validate_proof(self) -> "DetachedApplicationAuthorityAbsenceProof":
        _require_sha256(self.candidate_target_sha256, "authority target digest")
        _require_sha256(
            self.candidate_application_name_sha256,
            "authority application-name digest",
        )
        if not self.sources or self.source_count != len(self.sources):
            raise ValueError("Authority absence proof requires every bounded source.")
        if self.record_count != sum(source.record_count for source in self.sources):
            raise ValueError("Authority absence proof record count is inconsistent.")
        names = tuple(source.source for source in self.sources)
        if names != tuple(sorted(set(names))):
            raise ValueError("Authority absence proof sources must be unique and sorted.")
        return self


class DetachedApplicationRetirementMutationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_operation_key: str = ""
    reconciliation_key: str = ""
    provider_effect_phases: tuple[str, ...] = ()
    provider_effect_attempted: bool = False
    provider_effect_performed: bool = False
    provider_absence_verified: bool = False
    protected_targets_unchanged: bool = False
    authority_write_count: Literal[0] = 0
    error_code: str = ""
    error_message: str = Field(
        default="",
        max_length=MAX_DETACHED_APPLICATION_RETIREMENT_ERROR_MESSAGE_LENGTH,
    )


class DetachedApplicationRetirementRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    record_id: str
    plan_record_id: str
    mode: DetachedApplicationRetirementMode
    outcome: DetachedApplicationRetirementOutcome
    project_name: str
    environment_name: str
    application_name: str
    identity: DetachedApplicationRetirementIdentity
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
    candidate_observation: DetachedApplicationProviderObservation
    authority_absence_proof: DetachedApplicationAuthorityAbsenceProof
    protected_targets: tuple[DetachedApplicationProtectedTargetSnapshot, ...]
    mutation_evidence: DetachedApplicationRetirementMutationEvidence = Field(
        default_factory=DetachedApplicationRetirementMutationEvidence
    )
    authority_write_count: Literal[0] = 0

    @model_validator(mode="after")
    def _validate_record(self) -> "DetachedApplicationRetirementRecord":
        for field_name in (
            "record_id",
            "plan_record_id",
            "project_name",
            "environment_name",
            "application_name",
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
                raise ValueError(f"Detached application retirement record requires {field_name}.")
        for digest in (self.continuity_sha256, self.plan_sha256):
            _require_sha256(digest, "retirement record digest")
        protected_digests = tuple(target.target_id_sha256 for target in self.protected_targets)
        if not protected_digests or protected_digests != tuple(sorted(set(protected_digests))):
            raise ValueError(
                "Detached application protected snapshots must be non-empty, unique, and sorted."
            )
        if self.candidate_observation.target_id_sha256 in protected_digests:
            raise ValueError("Detached application candidate cannot be protected.")
        if self.mode == "plan":
            if self.outcome != "planned" or self.record_id != self.plan_record_id:
                raise ValueError("Detached application plans require one planned plan record.")
            if self.reviewed_plan_record_id or self.reviewed_plan_sha256:
                raise ValueError("Detached application plans reject reviewed-plan fields.")
        else:
            if self.outcome == "planned":
                raise ValueError("Detached application apply records require terminal outcomes.")
            if not self.completed_at.strip():
                raise ValueError("Detached application apply records require completed_at.")
            if self.reviewed_plan_record_id != self.plan_record_id:
                raise ValueError("Detached application apply must bind its plan record.")
            _require_sha256(self.reviewed_plan_sha256, "reviewed_plan_sha256")
            if self.reviewed_plan_sha256 != self.plan_sha256:
                raise ValueError("Detached application apply must bind its plan digest.")
        return self


def build_detached_application_retirement_record_id(*, trace_id: str, outcome: str) -> str:
    normalized_trace_id = trace_id.strip()
    normalized_outcome = outcome.strip()
    if not normalized_trace_id or not normalized_outcome:
        raise ValueError("Detached application retirement ids require trace and outcome.")
    return f"detached-application-retirement-{normalized_trace_id}-{normalized_outcome}"


def _require_sha256(value: str, field_name: str) -> None:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"Detached application retirement {field_name} must be SHA-256.")
