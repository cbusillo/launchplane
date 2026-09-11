from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.ordinary_agent import OrdinaryAgentPullRequest, OrdinaryAgentTarget
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentClaimedJob,
    OrdinaryAgentJobAttemptDisposition,
    OrdinaryAgentJobClaimFence,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentGuardedDeliveryFiniteRequestV2,
    OrdinaryAgentQualificationFiniteRequestV2,
)
from control_plane.ordinary_agent_job_dispatcher import build_ordinary_agent_job_dispatcher
from control_plane.ordinary_agent_worker_runtime import DEFAULT_ORDINARY_AGENT_WORKER_SUPPORT


class OrdinaryAgentJobDispatcherTests(unittest.TestCase):
    def setUp(self) -> None:
        self.target = OrdinaryAgentTarget(
            repository_id=42, repository="example/repo", base_branch="main"
        )
        self.common = {
            "request_id": "request-one",
            "idempotency_key": "idempotency-one",
            "principal_id": "agent_one",
            "session_id": "session-one",
            "lease_id": "lease-one",
            "target": self.target,
            "binding_revision": 1,
            "admitted_at": 1_800_000_000,
            "expires_at": 1_800_000_100,
            "continuation_expires_at": 1_800_000_200,
        }

    def _claimed(self, request: object) -> OrdinaryAgentClaimedJob:
        return OrdinaryAgentClaimedJob.model_validate(
            {
                "request": request,
                "claim_fence": OrdinaryAgentJobClaimFence(
                    request_id="request-one", worker_id="worker-one", generation=1
                ),
                "claim_expires_at": 1_800_000_050,
            }
        )

    def test_routes_each_persisted_variant_to_its_only_advancer(self) -> None:
        store = Mock()
        dispatcher = build_ordinary_agent_job_dispatcher(
            record_store=store, support=DEFAULT_ORDINARY_AGENT_WORKER_SUPPORT
        )
        disposition = OrdinaryAgentJobAttemptDisposition(
            status="waiting", next_due_at=1_800_000_010
        )
        qualification = OrdinaryAgentQualificationFiniteRequestV2.model_validate(self.common)
        guarded_v2 = OrdinaryAgentGuardedDeliveryFiniteRequestV2.model_validate(
            {
                **self.common,
                "base_sha": "a" * 40,
                "pull_requests": (OrdinaryAgentPullRequest(number=12, head_sha="b" * 40),),
                "permitted_stack_edit_pull_requests": (),
                "refresh_allowance_total": 1,
            }
        )
        guarded_v1_payload = guarded_v2.model_dump()
        guarded_v1_payload.pop("purpose")
        guarded_v1 = OrdinaryAgentFiniteRequestRecord.model_validate(
            {**guarded_v1_payload, "schema_version": 1},
        )

        with (
            patch(
                "control_plane.ordinary_agent_job_dispatcher.advance_ordinary_agent_qualification_job",
                return_value=disposition,
            ) as qualification_advance,
            patch(
                "control_plane.ordinary_agent_job_dispatcher.advance_ordinary_agent_merge_train_job",
                return_value=disposition,
            ) as guarded_advance,
        ):
            self.assertEqual(dispatcher(self._claimed(qualification)), disposition)
            self.assertEqual(dispatcher(self._claimed(guarded_v1)), disposition)
            self.assertEqual(dispatcher(self._claimed(guarded_v2)), disposition)

        qualification_advance.assert_called_once()
        self.assertIs(
            qualification_advance.call_args.kwargs["setup_resolver"],
            store.resolve_ordinary_agent_qualification_setup,
        )
        self.assertEqual(guarded_advance.call_count, 2)


if __name__ == "__main__":
    unittest.main()
