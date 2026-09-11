from __future__ import annotations

import unittest
from unittest.mock import Mock

from control_plane.contracts.ordinary_agent import OrdinaryAgentPullRequest
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentClaimedJob,
    OrdinaryAgentJobAttemptDisposition,
    OrdinaryAgentJobClaimRejected,
    OrdinaryAgentJobClaimFence,
    OrdinaryAgentJobCursor,
    OrdinaryAgentJobView,
    OrdinaryAgentJobWorkerStore,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
)
from control_plane.ordinary_agent_job_worker import (
    OrdinaryAgentJobScanState,
    run_ordinary_agent_job_once,
)
from tests.support.ordinary_agent_lifecycle import TARGET


class OrdinaryAgentJobWorkerTests(unittest.TestCase):
    def test_row_local_claim_rejection_advances_only_the_trusted_cursor(self) -> None:
        store = Mock(spec=OrdinaryAgentJobWorkerStore)
        store.claim_due_ordinary_agent_job.side_effect = OrdinaryAgentJobClaimRejected(
            cursor=OrdinaryAgentJobCursor(request_id="job-poison"),
            reason_code="request_variant_unsupported",
        )
        state = OrdinaryAgentJobScanState()
        advance = Mock()

        result = run_ordinary_agent_job_once(
            record_store=store,
            state=state,
            worker_id="worker-one",
            lease_seconds=30,
            advance_job=advance,
        )

        self.assertEqual(result.failure_phase, "claim")
        self.assertEqual(state.after, OrdinaryAgentJobCursor(request_id="job-poison"))
        advance.assert_not_called()

    def test_failed_job_does_not_starve_next_job_and_finish_status_is_authoritative(self) -> None:
        def claim(request_id: str) -> OrdinaryAgentClaimedJob:
            return OrdinaryAgentClaimedJob(
                request=OrdinaryAgentFiniteRequestRecord(
                    request_id=request_id,
                    idempotency_key=request_id,
                    principal_id="agent_one",
                    session_id="session_one",
                    lease_id="lease_one",
                    target=TARGET,
                    base_sha="a" * 40,
                    pull_requests=(OrdinaryAgentPullRequest(number=12, head_sha="b" * 40),),
                    permitted_stack_edit_pull_requests=(),
                    refresh_allowance_total=0,
                    admitted_at=10,
                    expires_at=100,
                ),
                claim_fence=OrdinaryAgentJobClaimFence(
                    request_id=request_id, worker_id="worker-one", generation=1
                ),
                claim_expires_at=50,
            )

        first, second = claim("job-a"), claim("job-b")
        store = Mock(spec=OrdinaryAgentJobWorkerStore)
        store.claim_due_ordinary_agent_job.side_effect = [first, second, None, first]
        store.finish_ordinary_agent_job_attempt.return_value = OrdinaryAgentJobView(
            request_id=second.request.request_id,
            principal_id=second.request.principal_id,
            session_id=second.request.session_id,
            target=TARGET,
            pull_request_numbers=(12,),
            expires_at=100,
            cancellation_requested=True,
            unresolved_effects=1,
            status="reconciliation_required",
            completed_effects=1,
            total_effects=2,
        )
        advance = Mock(
            side_effect=[
                RuntimeError("private-provider-credential-sentinel"),
                OrdinaryAgentJobAttemptDisposition(status="completed"),
                RuntimeError("private-provider-credential-sentinel"),
            ]
        )
        state = OrdinaryAgentJobScanState()
        results = [
            run_ordinary_agent_job_once(
                record_store=store,
                state=state,
                worker_id="worker-one",
                lease_seconds=30,
                advance_job=advance,
            )
            for _ in range(4)
        ]
        self.assertEqual([call.args[0] for call in advance.call_args_list], [first, second, first])
        cursors = [
            call.kwargs["after"] for call in store.claim_due_ordinary_agent_job.call_args_list
        ]
        self.assertEqual(
            [cursor.request_id if cursor else None for cursor in cursors],
            [None, "job-a", "job-b", None],
        )
        store.finish_ordinary_agent_job_attempt.assert_called_once_with(
            claim_fence=second.claim_fence,
            disposition=OrdinaryAgentJobAttemptDisposition(status="completed"),
        )
        self.assertEqual(results[1].status, "reconciliation_required")
        self.assertNotIn("private-provider-credential-sentinel", repr(results))
