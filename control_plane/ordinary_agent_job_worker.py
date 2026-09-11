"""One fair ordinary-job claim, independent of privileged-worker error accounting."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentClaimedJob,
    OrdinaryAgentJobAttemptDisposition,
    OrdinaryAgentJobClaimRejected,
    OrdinaryAgentJobCursor,
    OrdinaryAgentJobWorkerStore,
)


@dataclass
class OrdinaryAgentJobScanState:
    after: OrdinaryAgentJobCursor | None = None


@dataclass(frozen=True)
class OrdinaryAgentJobScanResult:
    processed: int = 0
    status: str | None = None
    failure_phase: Literal["claim", "advance_or_finish"] | None = None


def run_ordinary_agent_job_once(
    *,
    record_store: OrdinaryAgentJobWorkerStore,
    state: OrdinaryAgentJobScanState,
    worker_id: str,
    lease_seconds: int,
    advance_job: Callable[[OrdinaryAgentClaimedJob], OrdinaryAgentJobAttemptDisposition],
) -> OrdinaryAgentJobScanResult:
    """Run one supplied bounded controller step; this function grants no authority.

    The actual controller step must use joined authority and provider fences.
    Unexpected failures leave the coordination claim to expire; they never
    fabricate a semantic outcome or erase a checkpointed provider attempt.
    """
    try:
        claimed = record_store.claim_due_ordinary_agent_job(
            worker_id=worker_id, lease_seconds=lease_seconds, after=state.after
        )
    except OrdinaryAgentJobClaimRejected as error:
        state.after = error.cursor
        return OrdinaryAgentJobScanResult(failure_phase="claim")
    except Exception:
        return OrdinaryAgentJobScanResult(failure_phase="claim")
    if claimed is None:
        state.after = None
        return OrdinaryAgentJobScanResult()

    # Move even when this job fails, so unrelated later jobs are not starved.
    state.after = OrdinaryAgentJobCursor(request_id=claimed.request.request_id)
    try:
        disposition = advance_job(claimed)
        view = record_store.finish_ordinary_agent_job_attempt(
            claim_fence=claimed.claim_fence, disposition=disposition
        )
    except Exception:
        return OrdinaryAgentJobScanResult(processed=1, failure_phase="advance_or_finish")
    # Finish may observe cancellation or another authoritative outcome. Report
    # that public projection, never the controller's proposed disposition.
    return OrdinaryAgentJobScanResult(processed=1, status=view.status)
