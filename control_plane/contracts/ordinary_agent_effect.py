"""Closed internal effect/worker contracts. None are HTTP execution capabilities."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, TypeAlias
from dataclasses import fields
import json

from pydantic import Field, TypeAdapter, field_validator

from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget, StrictFrozenModel
from control_plane.contracts.ordinary_agent_custody import OrdinaryAgentCustodyCandidate
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
)
from control_plane.contracts.merge_train_effect import (
    MergeTrainEffectLineage,
    CandidateRefPrepareEffect,
    CandidateHeadMergeEffect,
    PullRequestHeadRefreshEffect,
    StackChildMergeEffect,
    PullRequestLandingEffect,
    StackChildCommentEffect,
    StackChildLabelEffect,
    StackChildCloseEffect,
    CandidateRefDeleteEffect,
)

from control_plane.contracts.merge_admission_record import (
    MergeAdmissionRecord,
    MergeLandingOutcomeRecord,
)
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_stack_collapse import MergeTrainStackCollapsePlanRecord

from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentMergeTrainSnapshotResult,
    OrdinaryAgentCandidateCheckResult,
    OrdinaryAgentProviderRequestCounts,
)

OrdinaryAgentProgressRecord: TypeAlias = (
    MergeTrainBatchCandidateRecord
    | MergeTrainBatchLandingPlanRecord
    | MergeTrainStackCollapsePlanRecord
)

MAX_CUSTODY_MINT_ATTEMPTS_PER_DISPATCH_CHILD = 3
MAX_CUSTODY_MINT_ATTEMPTS_PER_EFFECT = 9
MAX_SEMANTIC_DISPATCH_ATTEMPTS_PER_EFFECT = 3
MIN_PROVIDER_TOKEN_TTL_AT_DISPATCH_SECONDS = 120
EFFECT_DISPATCH_DB_LOCK_TIMEOUT_SECONDS = 5
ASYNC_PROVIDER_OBSERVATION_WINDOW_SECONDS = 120
MAX_ASYNC_PROVIDER_OBSERVATIONS = 3
MIN_RECONCILIATION_BACKOFF_SECONDS = 15
MAX_RECONCILIATION_OBSERVATIONS_PER_EFFECT = 3
MAX_RECONCILIATION_CUSTODY_MINTS_PER_EFFECT = 9
MAX_TOTAL_CUSTODY_MINT_ATTEMPTS_PER_EFFECT = 18
MAX_SNAPSHOT_PROVIDER_ATTEMPTS = 3
MAX_CANDIDATE_CHECK_OBSERVATIONS = 9
CANDIDATE_CHECK_DELAYS_SECONDS = (60, 120, 240, 480, 900)

Identifier = Annotated[str, Field(min_length=1, max_length=256)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Epoch = Annotated[int, Field(ge=0, le=2**63 - 1)]


class _SemanticCommand(StrictFrozenModel):
    @field_validator("effect", mode="before", check_fields=False)
    @classmethod
    def decode_persisted_effect(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        effect_type = cls.model_fields["effect"].annotation
        if effect_type is None:
            raise ValueError("semantic command has no effect type")
        if set(value) - {field.name for field in fields(effect_type)}:
            raise ValueError("unknown semantic effect field")
        lineage = value.get("lineage")
        if isinstance(lineage, dict) and set(lineage) - {
            field.name for field in fields(MergeTrainEffectLineage)
        }:
            raise ValueError("unknown semantic lineage field")
        # Strict JSON decoding reconstructs existing frozen dataclasses without
        # permitting scalar coercion or silently accepting future command fields.
        return TypeAdapter(effect_type).validate_json(json.dumps(value), strict=True)


class CandidateRefPrepareCommand(_SemanticCommand):
    kind: Literal["candidate_ref_prepare"] = "candidate_ref_prepare"
    effect: CandidateRefPrepareEffect


class CandidateHeadMergeCommand(_SemanticCommand):
    kind: Literal["candidate_head_merge"] = "candidate_head_merge"
    effect: CandidateHeadMergeEffect


class PullRequestHeadRefreshCommand(_SemanticCommand):
    kind: Literal["pull_request_head_refresh"] = "pull_request_head_refresh"
    effect: PullRequestHeadRefreshEffect


class StackChildMergeCommand(_SemanticCommand):
    kind: Literal["stack_child_merge"] = "stack_child_merge"
    effect: StackChildMergeEffect


class PullRequestLandingCommand(_SemanticCommand):
    kind: Literal["pull_request_landing"] = "pull_request_landing"
    effect: PullRequestLandingEffect


class StackChildCommentCommand(_SemanticCommand):
    kind: Literal["stack_child_comment"] = "stack_child_comment"
    effect: StackChildCommentEffect


class StackChildLabelCommand(_SemanticCommand):
    kind: Literal["stack_child_label"] = "stack_child_label"
    effect: StackChildLabelEffect


class StackChildCloseCommand(_SemanticCommand):
    kind: Literal["stack_child_close"] = "stack_child_close"
    effect: StackChildCloseEffect


class CandidateRefDeleteCommand(_SemanticCommand):
    kind: Literal["candidate_ref_delete"] = "candidate_ref_delete"
    effect: CandidateRefDeleteEffect


OrdinaryAgentSemanticCommand: TypeAlias = Annotated[
    CandidateRefPrepareCommand
    | CandidateHeadMergeCommand
    | PullRequestHeadRefreshCommand
    | StackChildMergeCommand
    | PullRequestLandingCommand
    | StackChildCommentCommand
    | StackChildLabelCommand
    | StackChildCloseCommand
    | CandidateRefDeleteCommand,
    Field(discriminator="kind"),
]


class OrdinaryAgentControllerFence(StrictFrozenModel):
    controller_key: Identifier
    lease_owner: Identifier
    lease_acquired_at: Identifier


EffectState = Literal[
    "reserved",
    "dispatching",
    "waiting_provider",
    "not_dispatched",
    "completed",
    "completed_observed",
    "rebind_pending",
    "terminal_conflict",
    "exhausted",
    "reconciliation_required",
    "retained_no_conditional_delete",
]


class OrdinaryAgentEffectRecord(StrictFrozenModel):
    schema_version: Literal[1] = 1
    effect_id: Identifier
    request_id: Identifier
    session_id: Identifier
    lease_id: Identifier
    principal_id: Identifier
    scope_sha256: Digest
    binding_revision: int = Field(ge=1)
    semantic_ordinal: int = Field(ge=1)
    action_ordinal: int = Field(ge=1)
    command_sha256: Digest
    command: OrdinaryAgentSemanticCommand
    target: OrdinaryAgentTarget
    controller_fence: OrdinaryAgentControllerFence
    policy_record_id: Identifier
    policy_revision: int = Field(ge=1)
    policy_sha256: Digest
    credential_id: Identifier
    credential_version: int = Field(ge=1)
    credential_digest: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    state: EffectState = "reserved"
    revision: int = Field(default=1, ge=1)
    reserved_at: Epoch
    updated_at: Epoch
    dispatch_count: int = Field(default=0, ge=0)
    dispatch_custody_count: int = Field(default=0, ge=0)
    reconciliation_count: int = Field(default=0, ge=0)
    reconciliation_custody_count: int = Field(default=0, ge=0)
    next_observation_at: Epoch | None = None
    reason_code: str | None = Field(default=None, max_length=128)
    rebound_revision: int | None = Field(default=None, ge=1)


class OrdinaryAgentCustodyAttemptReservation(StrictFrozenModel):
    effect_id: Identifier
    effect_revision: int = Field(ge=1)
    purpose: Literal["dispatch", "reconciliation"]
    semantic_ordinal: int = Field(ge=1)
    custody_ordinal: int = Field(ge=1)
    attempt_id: Identifier
    idempotency_key: Identifier
    candidate: OrdinaryAgentCustodyCandidate = Field(repr=False)

    @property
    def request_payload(self) -> dict[str, object]:
        return {
            "effect_id": self.effect_id,
            "purpose": self.purpose,
            "semantic_ordinal": self.semantic_ordinal,
            "custody_ordinal": self.custody_ordinal,
        }


class OrdinaryAgentSemanticDispatchAttemptRecord(StrictFrozenModel):
    schema_version: Literal[1] = 1
    child_id: Identifier
    effect_id: Identifier
    semantic_ordinal: int = Field(ge=1)
    custody_attempt_id: Identifier
    dispatch_checkpoint_at: Epoch
    fixed_token_expires_at: Epoch
    controller_fence: OrdinaryAgentControllerFence
    command_sha256: Digest


class OrdinaryAgentCompletedOutcome(StrictFrozenModel):
    kind: Literal["completed"] = "completed"
    result_sha: str | None = Field(default=None, max_length=64)
    result_id: str | None = Field(default=None, max_length=256)
    no_op: bool = False
    proof: OrdinaryAgentRefObservation | OrdinaryAgentPullRequestObservation | None = None


class OrdinaryAgentKnownNotDispatchedOutcome(StrictFrozenModel):
    kind: Literal["known_not_dispatched"] = "known_not_dispatched"
    reason: Literal["local_ttl", "transport_not_sent", "provider_rejected", "non_mergeable"]


class OrdinaryAgentUnknownOutcome(StrictFrozenModel):
    kind: Literal["unknown"] = "unknown"
    reason: Literal["transport_ambiguous", "process_interrupted", "response_ambiguous"]


class OrdinaryAgentAcceptedAsyncOutcome(StrictFrozenModel):
    kind: Literal["accepted_async"] = "accepted_async"


OrdinaryAgentSemanticOutcome: TypeAlias = Annotated[
    OrdinaryAgentCompletedOutcome
    | OrdinaryAgentKnownNotDispatchedOutcome
    | OrdinaryAgentUnknownOutcome
    | OrdinaryAgentAcceptedAsyncOutcome,
    Field(discriminator="kind"),
]


class OrdinaryAgentRefObservation(StrictFrozenModel):
    kind: Literal["ref"] = "ref"
    repository: Identifier
    ref: Identifier
    sha: str | None = Field(default=None, max_length=64)
    parents: tuple[str, ...] = ()
    tree_sha: str | None = Field(default=None, max_length=64)
    contained_head_sha: str | None = Field(default=None, max_length=64)
    commit_message: str = Field(default="", max_length=8192)

    @field_validator("parents", mode="before")
    @classmethod
    def read_parents(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class OrdinaryAgentPullRequestObservation(StrictFrozenModel):
    kind: Literal["pull_request"] = "pull_request"
    repository: Identifier
    number: int = Field(gt=0)
    head_sha: str = Field(min_length=1, max_length=64)
    base_ref: Identifier
    base_sha: str = Field(min_length=1, max_length=64)
    state: Literal["open", "closed"]
    merged: bool
    merge_commit_sha: str | None = Field(default=None, max_length=64)
    merge_commit_tree_sha: str | None = Field(default=None, max_length=64)
    merge_commit_parents: tuple[str, ...] = ()
    head_parents: tuple[str, ...] = ()

    @field_validator("head_parents", "merge_commit_parents", mode="before")
    @classmethod
    def read_parents(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class OrdinaryAgentCommentObservation(StrictFrozenModel):
    kind: Literal["comment"] = "comment"
    repository: Identifier
    number: int = Field(gt=0)
    matching_comment_id: str | None = Field(default=None, max_length=256)
    matching_body: str | None = Field(default=None, max_length=65536)
    pages_read: int = Field(ge=1, le=3)
    exhausted: bool


class OrdinaryAgentLabelObservation(StrictFrozenModel):
    kind: Literal["label"] = "label"
    repository: Identifier
    number: int = Field(gt=0)
    label: str = Field(min_length=1, max_length=256)
    present: bool


OrdinaryAgentProviderObservation: TypeAlias = Annotated[
    OrdinaryAgentRefObservation
    | OrdinaryAgentPullRequestObservation
    | OrdinaryAgentCommentObservation
    | OrdinaryAgentLabelObservation,
    Field(discriminator="kind"),
]


class OrdinaryAgentReconciliationObservation(StrictFrozenModel):
    observation_id: Identifier
    custody_attempt_id: Identifier
    observed_at: Epoch
    observation: OrdinaryAgentProviderObservation


class OrdinaryAgentProviderQuotaKey(StrictFrozenModel):
    provider: Literal["github"] = "github"
    authority_kind: Literal["app", "installation"]
    authority_id: int = Field(gt=0, le=2**63 - 1)
    resource_class: Literal["core", "search", "graphql", "secondary"]


class OrdinaryAgentProviderWaitObservation(StrictFrozenModel):
    retry_not_before: Epoch
    classification: Literal["primary_rate_limit", "secondary_rate_limit"]


class OrdinaryAgentProviderWaitRecord(StrictFrozenModel):
    quota_key: OrdinaryAgentProviderQuotaKey
    retry_not_before: Epoch
    observed_at: Epoch
    classification: Literal["primary_rate_limit", "secondary_rate_limit"]


class OrdinaryAgentJobCursor(StrictFrozenModel):
    request_id: Identifier


class OrdinaryAgentJobClaimFence(StrictFrozenModel):
    request_id: Identifier
    worker_id: Identifier
    generation: int = Field(ge=1)


class OrdinaryAgentClaimedJob(StrictFrozenModel):
    request: OrdinaryAgentFiniteRequestRecord
    claim_fence: OrdinaryAgentJobClaimFence
    claim_expires_at: Epoch
    controller_fence: OrdinaryAgentControllerFence | None = None


class OrdinaryAgentJobAttemptDisposition(StrictFrozenModel):
    status: Literal["waiting", "blocked", "completed", "reconciliation_required"]
    next_due_at: Epoch | None = None
    reason_code: str | None = Field(default=None, max_length=128)


class OrdinaryAgentJobView(StrictFrozenModel):
    schema_version: Literal[1] = 1
    request_id: Identifier
    principal_id: Identifier
    session_id: Identifier
    target: OrdinaryAgentTarget
    pull_request_numbers: tuple[int, ...]
    expires_at: Epoch
    continuation_expires_at: Epoch | None = None
    cancellation_requested: bool = False
    unresolved_effects: int = Field(ge=0)
    status: Literal[
        "pending",
        "running",
        "waiting",
        "blocked",
        "cancelled",
        "partially_completed",
        "completed",
        "reconciliation_required",
    ]
    next_due_at: Epoch | None = None
    reason_code: str | None = Field(default=None, max_length=128)
    completed_effects: int = Field(ge=0)
    total_effects: int = Field(ge=0)

    @field_validator("pull_request_numbers", mode="before")
    @classmethod
    def read_numbers(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class OrdinaryAgentSnapshotAttemptRecord(StrictFrozenModel):
    schema_version: Literal[1] = 1
    attempt_id: Identifier
    request_id: Identifier
    binding_revision: int = Field(ge=1)
    scope_sha256: Digest
    principal_id: Identifier
    credential_id: Identifier
    credential_version: int = Field(ge=1)
    purpose: Literal["snapshot", "candidate_check"]
    attempt_ordinal: int = Field(ge=1)
    candidate_sha: str = Field(default="", max_length=64)
    controller_fence: OrdinaryAgentControllerFence
    state: Literal["reserved", "reading", "completed", "incomplete", "fenced", "exhausted"] = (
        "reserved"
    )
    revision: int = Field(default=1, ge=1)
    created_at: Epoch
    updated_at: Epoch
    custody_attempt_ids: tuple[str, ...] = ()
    result: OrdinaryAgentMergeTrainSnapshotResult | OrdinaryAgentCandidateCheckResult | None = None
    failure_counts: OrdinaryAgentProviderRequestCounts | None = None
    reason_code: str | None = Field(default=None, max_length=128)
    next_due_at: Epoch | None = None

    @field_validator("custody_attempt_ids", mode="before")
    @classmethod
    def read_ids(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value


class OrdinaryAgentReadCustodyReservation(StrictFrozenModel):
    read_attempt_id: Identifier
    attempt_revision: int = Field(ge=1)
    custody_attempt_id: Identifier
    custody_ordinal: int = Field(ge=1)
    purpose: Literal["snapshot", "candidate_check"]
    idempotency_key: Identifier
    candidate: OrdinaryAgentCustodyCandidate = Field(repr=False)

    @property
    def request_payload(self) -> dict[str, object]:
        return {
            "read_attempt_id": self.read_attempt_id,
            "purpose": self.purpose,
            "custody_ordinal": self.custody_ordinal,
        }


class OrdinaryAgentSnapshotStore(Protocol):
    def reserve_ordinary_agent_snapshot_attempt(
        self,
        *,
        request_id: str,
        expected_binding_revision: int,
        controller_fence: OrdinaryAgentControllerFence,
    ) -> OrdinaryAgentSnapshotAttemptRecord: ...

    def reserve_ordinary_agent_candidate_check_attempt(
        self,
        *,
        request_id: str,
        expected_binding_revision: int,
        controller_fence: OrdinaryAgentControllerFence,
        candidate_sha: str,
    ) -> OrdinaryAgentSnapshotAttemptRecord: ...

    def reserve_ordinary_agent_read_custody_attempt(
        self,
        *,
        attempt_id: str,
        expected_attempt_revision: int,
    ) -> OrdinaryAgentReadCustodyReservation: ...

    def record_ordinary_agent_snapshot_success(
        self,
        *,
        attempt_id: str,
        custody_attempt_id: str,
        result: OrdinaryAgentMergeTrainSnapshotResult,
    ) -> OrdinaryAgentSnapshotAttemptRecord: ...

    def record_ordinary_agent_candidate_check_success(
        self,
        *,
        attempt_id: str,
        custody_attempt_id: str,
        result: OrdinaryAgentCandidateCheckResult,
    ) -> OrdinaryAgentSnapshotAttemptRecord: ...

    def record_ordinary_agent_read_failure(
        self,
        *,
        attempt_id: str,
        custody_attempt_id: str,
        reason_code: Literal[
            "provider_wait",
            "provider_incomplete",
            "provider_transport",
            "snapshot_query_cost_exceeded",
            "cleanup_unknown",
        ],
        counts: OrdinaryAgentProviderRequestCounts,
    ) -> OrdinaryAgentSnapshotAttemptRecord: ...


class OrdinaryAgentJobWorkerStore(Protocol):
    def claim_due_ordinary_agent_job(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        after: OrdinaryAgentJobCursor | None = None,
    ) -> OrdinaryAgentClaimedJob | None: ...

    def finish_ordinary_agent_job_attempt(
        self,
        *,
        claim_fence: OrdinaryAgentJobClaimFence,
        disposition: OrdinaryAgentJobAttemptDisposition,
    ) -> OrdinaryAgentJobView: ...


class OrdinaryAgentControllerStore(Protocol):
    def create_ordinary_merge_landing_outcome_record_if_absent(
        self,
        *,
        request_id: str,
        expected_binding_revision: int,
        record: MergeLandingOutcomeRecord,
    ) -> tuple[MergeLandingOutcomeRecord, bool]: ...
    def retire_ordinary_agent_job_history(
        self, *, claim_fence: OrdinaryAgentJobClaimFence
    ) -> OrdinaryAgentJobView: ...

    def create_ordinary_merge_admission_record_if_absent(
        self,
        *,
        request_id: str,
        expected_binding_revision: int,
        controller_fence: OrdinaryAgentControllerFence,
        record: MergeAdmissionRecord,
    ) -> tuple[MergeAdmissionRecord, bool]: ...

    def rebind_ordinary_agent_after_head_refresh(
        self,
        *,
        effect_id: str,
        expected_effect_revision: int,
        controller_fence: OrdinaryAgentControllerFence,
    ) -> OrdinaryAgentFiniteRequestRecord: ...

    def acquire_ordinary_merge_train_controller_state_record(
        self,
        *,
        claim_fence: OrdinaryAgentJobClaimFence,
        expected_binding_revision: int,
        policy_key: str,
        policy_sha256: str,
        lease_seconds: int,
        initial_active_action: str,
        initial_active_phase: str,
        adoptable_active_actions: tuple[str, ...],
    ) -> MergeTrainControllerStateRecord: ...

    def compare_and_set_ordinary_merge_train_controller_state_record(
        self,
        *,
        request_id: str,
        expected_binding_revision: int,
        controller_fence: OrdinaryAgentControllerFence,
        record: MergeTrainControllerStateRecord,
        lease_seconds: int,
    ) -> MergeTrainControllerStateRecord: ...

    def write_ordinary_merge_train_record(
        self,
        *,
        request_id: str,
        expected_binding_revision: int,
        controller_fence: OrdinaryAgentControllerFence,
        record: OrdinaryAgentProgressRecord,
        expected_predecessor_record_id: str | None = None,
    ) -> OrdinaryAgentProgressRecord: ...

    def yield_ordinary_merge_train_controller_state_record(
        self,
        *,
        request_id: str,
        expected_binding_revision: int,
        controller_fence: OrdinaryAgentControllerFence,
    ) -> MergeTrainControllerStateRecord: ...


class OrdinaryAgentEffectStore(Protocol):
    def reserve_ordinary_agent_effect(
        self,
        *,
        request_id: str,
        expected_binding_revision: int,
        controller_fence: OrdinaryAgentControllerFence,
        command: OrdinaryAgentSemanticCommand,
        semantic_ordinal: int,
    ) -> OrdinaryAgentEffectRecord: ...
    def reserve_ordinary_custody_attempt(
        self, *, effect_id: str, expected_effect_revision: int
    ) -> OrdinaryAgentCustodyAttemptReservation: ...
    def reserve_ordinary_reconciliation_custody_attempt(
        self, *, effect_id: str, expected_effect_revision: int
    ) -> OrdinaryAgentCustodyAttemptReservation: ...
    def checkpoint_ordinary_semantic_dispatch(
        self,
        *,
        effect_id: str,
        controller_fence: OrdinaryAgentControllerFence,
        custody_attempt_id: str,
        fixed_token_expires_at: int,
    ) -> OrdinaryAgentSemanticDispatchAttemptRecord: ...
    def record_ordinary_semantic_outcome(
        self, *, child_id: str, typed_outcome: OrdinaryAgentSemanticOutcome
    ) -> OrdinaryAgentEffectRecord: ...
    def append_ordinary_effect_reconciliation(
        self, *, child_id: str, typed_observation: OrdinaryAgentReconciliationObservation
    ) -> OrdinaryAgentEffectRecord: ...
    def complete_ordinary_effect_without_dispatch(
        self,
        *,
        effect_id: str,
        expected_effect_revision: int,
        disposition: Literal[
            "label_already_present", "candidate_ref_retained_no_conditional_delete"
        ],
        typed_observation: OrdinaryAgentReconciliationObservation | None = None,
    ) -> OrdinaryAgentEffectRecord: ...
    def read_ordinary_agent_effect(self, *, effect_id: str) -> OrdinaryAgentEffectRecord: ...
    def record_provider_wait(
        self,
        *,
        quota_key: OrdinaryAgentProviderQuotaKey,
        observation: OrdinaryAgentProviderWaitObservation,
    ) -> OrdinaryAgentProviderWaitRecord: ...
    def read_provider_wait(
        self,
        *,
        quota_key: OrdinaryAgentProviderQuotaKey,
    ) -> OrdinaryAgentProviderWaitRecord | None: ...


OrdinaryAgentCompletedOutcome.model_rebuild()
