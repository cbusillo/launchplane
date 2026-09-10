"""Atomic ordinary landing completion without a provider dispatch."""

from typing import Literal, Protocol

from control_plane.contracts.merge_admission_record import (
    MergeAdmissionProposal,
    MergeAdmissionRecord,
    MergeLandingOutcomeRecord,
)
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingPlanRecord
from control_plane.contracts.ordinary_agent import StrictFrozenModel
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentControllerFence,
    OrdinaryAgentLandingPreparation,
)


class OrdinaryAgentNoOpLandingFinalization(StrictFrozenModel):
    disposition: Literal["created", "replay"]
    preparation: OrdinaryAgentLandingPreparation
    admission: MergeAdmissionRecord
    outcome: MergeLandingOutcomeRecord
    successor: MergeTrainBatchLandingPlanRecord


class OrdinaryAgentNoOpLandingStore(Protocol):
    def finalize_ordinary_no_op_landing_preparation(
        self,
        *,
        preparation_id: str,
        expected_revision: int,
        controller_fence: OrdinaryAgentControllerFence,
        proposal: MergeAdmissionProposal,
        custody_attempt_id: str,
        successor: MergeTrainBatchLandingPlanRecord,
    ) -> OrdinaryAgentNoOpLandingFinalization: ...

    def read_ordinary_no_op_landing_finalization(
        self, *, preparation_id: str
    ) -> OrdinaryAgentNoOpLandingFinalization | None: ...
