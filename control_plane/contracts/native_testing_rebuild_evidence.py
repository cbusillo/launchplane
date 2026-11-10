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
    "candidate_snapshot",
    "predecessor_snapshot",
    "checkpoint_probe",
    "deployment_record",
    "forward_route_receipt",
    "inverse_route_receipt",
    "publication_receipt",
    "rollback_checkpoint",
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
        "checkpoint_snapshot_only",
        "checkpoint_probe_only",
        "intent_only",
        "candidate_probe_not_retained",
    ] = "not_read"
    state: NativeEvidenceState | None = None
    observed_at: datetime | None = None
    attempt: int | None = Field(default=None, ge=1)
    verified: bool | None = None
    runtime_identity_matches: bool | None = None


class NativeTestingRebuildEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    coverage: Literal["retained_original_records_only"] = "retained_original_records_only"
    operation_attempt: int = Field(ge=0)
    checkpoint_attempt_binding: Literal["not_recorded"] = "not_recorded"
    reservation: NativeTestingEvidenceItem
    original_intent: NativeTestingEvidenceItem
    checkpoint: NativeTestingEvidenceItem
    terminal_probe: NativeTestingEvidenceItem
    candidate: NativeTestingEvidenceItem
    predecessor: NativeTestingEvidenceItem
    deployment: NativeTestingEvidenceItem
    forward_route: NativeTestingEvidenceItem
    inverse_route: NativeTestingEvidenceItem
    selection: NativeTestingEvidenceItem
    rollback: NativeTestingEvidenceItem
    publication_started: bool | None = None
    retirement_started: bool | None = None
    rollback_started: bool | None = None
    provider_effects: Literal["unknown"] = "unknown"
    needed_copies: Literal["retain_all"] = "retain_all"
    safe_to_release: Literal[False] = False
