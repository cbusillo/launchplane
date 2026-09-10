"""Use the existing controller checkpoint for one atomic no-op completion."""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_admission_record import MergeAdmissionFenceRejectedError
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentControllerFence,
    OrdinaryAgentLandingPreparation,
    OrdinaryAgentProgressRecord,
)
from control_plane.contracts.ordinary_agent_noop import (
    OrdinaryAgentNoOpLandingFinalization,
    OrdinaryAgentNoOpLandingStore,
)
from control_plane.merge_admission import GuardedMergeAdmission, MergeAdmissionDeniedError
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied


class OrdinaryNoOpFinalizationUnavailable(PermissionError):
    def __init__(self) -> None:
        super().__init__("ordinary candidate no-op requires joined no-op finalization")


@dataclass(frozen=True)
class OrdinaryNoOpLandingContext:
    preparation: OrdinaryAgentLandingPreparation
    guard: GuardedMergeAdmission
    entry: MergeTrainBatchLandingEntry
    custody_attempt_id: str


@dataclass
class OrdinaryNoOpLandingRoute:
    store: OrdinaryAgentNoOpLandingStore
    _context: OrdinaryNoOpLandingContext | None = field(default=None, init=False)
    _consumed: bool = field(default=False, init=False)
    _result: OrdinaryAgentNoOpLandingFinalization | None = field(default=None, init=False)

    @property
    def armed(self) -> bool:
        # Keep the slot armed after consumption until its scope exits, so a
        # second write cannot silently fall through to ordinary persistence.
        return self._context is not None

    @contextmanager
    def arm(self, context: OrdinaryNoOpLandingContext) -> Iterator[None]:
        if self.armed:
            raise OrdinaryAgentSessionAdmissionDenied("no_op_checkpoint_conflict")
        self._context, self._consumed, self._result = context, False, None
        try:
            yield
            if self._result is None:
                raise OrdinaryAgentSessionAdmissionDenied("no_op_checkpoint_missing")
        finally:
            self._context, self._consumed, self._result = None, False, None

    def finalize(
        self,
        *,
        record: OrdinaryAgentProgressRecord,
        controller_fence: OrdinaryAgentControllerFence,
        predecessor_record_id: str,
    ) -> MergeTrainBatchLandingPlanRecord:
        context = self._context
        if context is None or self._consumed:
            raise OrdinaryAgentSessionAdmissionDenied("no_op_checkpoint_conflict")
        self._consumed = True
        preparation = context.preparation
        evidence = preparation.evidence
        if (
            not isinstance(record, MergeTrainBatchLandingPlanRecord)
            or predecessor_record_id != preparation.landing_plan_record_id
            or controller_fence != preparation.controller_fence
            or evidence is None
            or context.entry.status != "skipped"
            or context.custody_attempt_id != preparation.custody_attempt_id
            or context.guard.landing_plan_record.record_id != predecessor_record_id
            or record.record_id == predecessor_record_id
            or record.status != "active"
            or record.ordinary_job_binding != context.guard.landing_plan_record.ordinary_job_binding
            or record.landing_plan.plan_id != context.guard.landing_plan_record.landing_plan.plan_id
            or tuple(
                entry
                for entry in record.landing_plan.entries
                if entry.pull_request_number == context.entry.pull_request_number
            )
            != (context.entry,)
        ):
            raise OrdinaryAgentSessionAdmissionDenied("no_op_checkpoint_conflict")
        # The core renews its lease before this write. Build the proposal now,
        # using the guard's current controller provider, rather than freezing a
        # lease expiry before the checkpoint that will renew it.
        proposal = context.guard.build_proposal(
            entry=preparation.entry,
            observed_base_sha=evidence.base_identity.sha,
            observed_base_tree_sha=evidence.base_identity.tree_sha,
            observed_head_sha=evidence.repository_evidence.target.head_sha,
            observed_head_tree_sha=evidence.repository_evidence.target.tree_sha,
        )
        try:
            result = self.store.finalize_ordinary_no_op_landing_preparation(
                preparation_id=preparation.preparation_id,
                expected_revision=preparation.revision,
                controller_fence=controller_fence,
                proposal=proposal,
                custody_attempt_id=context.custody_attempt_id,
                successor=record,
            )
        except MergeAdmissionFenceRejectedError as error:
            raise MergeAdmissionDeniedError(
                "The controller fence no longer admits this no-op completion",
                reason_code="controller_fence_rejected",
            ) from error
        except OrdinaryAgentSessionAdmissionDenied as error:
            # Only expected evidence changes become a normal blocked result.
            # Conflicting successors, replay/binding errors and persistence
            # failures remain unexpected failures for diagnosis and recovery.
            reason = {
                "landing_authority_changed": "controller_authority_changed",
                "landing_evidence_expired": "merge_readiness_not_ready",
            }.get(error.reason_code)
            if reason is None:
                raise
            raise MergeAdmissionDeniedError(
                "Current evidence does not admit this no-op completion", reason_code=reason
            ) from error
        if result.successor != record:
            raise OrdinaryAgentSessionAdmissionDenied("no_op_checkpoint_conflict")
        self._result = result
        return result.successor
