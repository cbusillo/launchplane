from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.idempotency_record import LaunchplaneMutationReservationState
from control_plane.contracts.promotion_record import ReleaseStatus
from control_plane.contracts.testing_rebuild import TestingRebuildStep


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
