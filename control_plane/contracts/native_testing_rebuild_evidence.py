from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.contracts.idempotency_record import LaunchplaneMutationReservationState
from control_plane.contracts.promotion_record import ReleaseStatus, HealthFailureKind
from control_plane.contracts.testing_rebuild import TestingRebuildStep
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.copy_readback_diagnostics import CopyFailureStage


NativeEvidenceAvailability = Literal[
    "present", "missing", "null", "unavailable", "unsupported", "unbound", "not_retained"
]
NativeEvidenceSource = Literal[
    "native_reservation",
    "native_original_intent",
    "native_checkpoint",
    "checkpoint_preparation",
    "checkpoint_private_readiness",
    "checkpoint_probe",
    "deployment_record",
    "forward_route_receipt",
    "inverse_route_receipt",
    "publication_receipt",
]
NativeEvidenceState = (
    LaunchplaneMutationReservationState
    | TestingRebuildStep
    | ReleaseStatus
    | Literal["planned", "applied", "reversed"]
)


class NativeTestingEvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: NativeEvidenceSource
    availability: NativeEvidenceAvailability = "unsupported"
    binding: Literal["matched", "unverified", "mismatched"] = "unverified"
    coverage: Literal[
        "not_read",
        "retained_record_only",
        "checkpoint_probe_only",
        "candidate_probe_not_retained",
    ] = "not_read"
    state: NativeEvidenceState | None = None
    observed_at: datetime | None = None
    attempt: int | None = Field(default=None, ge=1)
    http_status: int | None = Field(
        default=None, ge=100, le=599, exclude_if=lambda value: value is None
    )
    failure_kind: HealthFailureKind | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    verified: bool | None = None
    runtime_identity_matches: bool | None = None
    effect_checkpoint_recorded: bool | None = None
    not_dispatched: bool | None = None
    route_write_not_started: bool | None = None


class NativeTestingRebuildEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    operation_attempt: int = Field(ge=0)
    reservation: NativeTestingEvidenceItem
    original_intent: NativeTestingEvidenceItem
    checkpoint: NativeTestingEvidenceItem
    terminal_probe: NativeTestingEvidenceItem
    candidate_preparation: NativeTestingEvidenceItem
    candidate_readiness: NativeTestingEvidenceItem
    predecessor_snapshot: Literal["identity_recorded", "stopped", "absent"] | None = None
    deployment: NativeTestingEvidenceItem
    forward_route: NativeTestingEvidenceItem
    inverse_route: NativeTestingEvidenceItem
    selection: NativeTestingEvidenceItem
    publication_started: bool | None = None
    retirement_started: bool | None = None
    rollback_started: bool | None = None
    needed_copies: Literal["retain_all"] = "retain_all"


class NativeCandidateSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    namespace: str = Field(pattern=r"^lp-testing-[a-f0-9]{32}$")
    server_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    project_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    environment_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    compose_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    resources_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    runtime_identity: RuntimeIdentity


class NativeRouteSelector(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    server_id: str = Field(pattern=r"^[a-zA-Z0-9_-]+$")
    copy_id: str = Field(min_length=1)
    route_id: str = Field(pattern=r"^lp-testing-route-[a-f0-9]{32}$")
    route_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    document_sha256: str = Field(pattern=r"^(absent|[a-f0-9]{64})$")
    backend: str | None

    @field_validator("backend")
    @classmethod
    def validate_backend(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("A retained backend must be an HTTP origin without credentials.")
        return value


class NativePrivateBindingItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    availability: NativeEvidenceAvailability = "unsupported"
    binding: Literal["matched", "unverified", "mismatched"] = "unverified"
    coverage: Literal["not_read", "retained_record_only"] = "not_read"
    observed_at: datetime | None = None
    attempt: int | None = Field(default=None, ge=1)


class NativeCandidateRead(NativePrivateBindingItem):
    selector: NativeCandidateSelector | None = None


class NativeRouteRead(NativePrivateBindingItem):
    selector: NativeRouteSelector | None = None
    route_digest_matches: bool | None = None
    observed_backend_matches: bool | None = None
    backend_observed_at: datetime | None = None


class NativeFailureRead(NativePrivateBindingItem):
    evidence_id: str | None = Field(default=None, pattern=r"^(unavailable|[a-f0-9]{32})$")
    stage: CopyFailureStage | None = None
    kind: Literal["command_failed", "refused", "unavailable"] | None = None
    exit_status: int | None = Field(default=None, ge=-255, le=255)


class NativeTestingExecutionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    operation_id: str
    product: str
    context: str
    instance: str
    native_evidence: NativeTestingRebuildEvidence
    reservation_id: str | None = None
    intent_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    checkpoint_plan_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    execution_server_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]+$")
    candidate: NativeCandidateRead = Field(default_factory=NativeCandidateRead)
    forward_route: NativeRouteRead = Field(default_factory=NativeRouteRead)
    inverse_route: NativeRouteRead = Field(default_factory=NativeRouteRead)
    terminal_account: Literal["not_retained"] = "not_retained"
    failure_reference: NativeFailureRead = Field(default_factory=NativeFailureRead)
    execution_trace: Literal["not_retained"] = "not_retained"
    submitted_program: Literal["not_retained"] = "not_retained"
