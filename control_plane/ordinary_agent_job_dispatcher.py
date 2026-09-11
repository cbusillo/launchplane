"""Exhaustive composition-time routing for persisted ordinary jobs."""

from __future__ import annotations

from typing import Literal, Protocol, assert_never, cast

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentClaimedJob,
    OrdinaryAgentJobAttemptDisposition,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentGuardedDeliveryFiniteRequestV2,
    OrdinaryAgentQualificationFiniteRequestV2,
)
from control_plane.ordinary_agent_merge_train_job import (
    OrdinaryAgentMergeTrainJobStore,
    advance_ordinary_agent_merge_train_job,
)
from control_plane.ordinary_agent_qualification_job import (
    _QualificationStore,
    advance_ordinary_agent_qualification_job,
)
from control_plane.contracts.ordinary_agent_qualification import OrdinaryAgentQualificationSetup
from control_plane.ordinary_agent_worker_runtime import (
    OrdinaryAgentJobAdvancer,
    OrdinaryAgentWorkerCompatibilityError,
    OrdinaryAgentWorkerSupportDescriptor,
)


class _QualificationRecordStore(_QualificationStore, Protocol):
    def resolve_ordinary_agent_qualification_setup(
        self, *, request: OrdinaryAgentQualificationFiniteRequestV2
    ) -> OrdinaryAgentQualificationSetup: ...


def build_ordinary_agent_job_dispatcher(
    *, record_store: object, support: OrdinaryAgentWorkerSupportDescriptor
) -> OrdinaryAgentJobAdvancer:
    """Bind all supported persisted request variants to their domain controller."""
    support.validate()
    merge_store = cast(OrdinaryAgentMergeTrainJobStore, record_store)
    qualification_store = cast(_QualificationRecordStore, record_store)

    def dispatch(claimed: OrdinaryAgentClaimedJob) -> OrdinaryAgentJobAttemptDisposition:
        request = claimed.request
        purpose: Literal["qualification", "guarded_delivery"] = (
            "qualification"
            if isinstance(request, OrdinaryAgentQualificationFiniteRequestV2)
            else "guarded_delivery"
        )
        if not support.supports_phase(request=request, purpose=purpose):
            raise OrdinaryAgentWorkerCompatibilityError(
                "Unsupported ordinary finite request phase: "
                f"schema={request.schema_version}, purpose={purpose}."
            )
        if isinstance(request, OrdinaryAgentQualificationFiniteRequestV2):
            return advance_ordinary_agent_qualification_job(
                claimed=claimed,
                store=qualification_store,
                setup_resolver=qualification_store.resolve_ordinary_agent_qualification_setup,
            )
        if isinstance(
            request,
            (OrdinaryAgentFiniteRequestRecord, OrdinaryAgentGuardedDeliveryFiniteRequestV2),
        ):
            return advance_ordinary_agent_merge_train_job(claimed=claimed, store=merge_store)
        assert_never(request)

    return dispatch
