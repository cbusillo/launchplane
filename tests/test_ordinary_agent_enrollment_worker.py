from __future__ import annotations

import unittest
from unittest.mock import Mock

from control_plane.contracts.ordinary_agent_lifecycle import (
    OrdinaryAgentEnrollmentApprovalReference,
)
from control_plane.ordinary_agent_enrollment_worker import (
    OrdinaryAgentEnrollmentRecoveryState,
    recover_ordinary_agent_enrollments_once,
)
from control_plane.storage.postgres import PostgresRecordStore


class OrdinaryAgentEnrollmentRecoveryTests(unittest.TestCase):
    def test_failed_pages_advance_and_wrap_instead_of_starving_later_requests(self) -> None:
        store = Mock(spec=PostgresRecordStore)
        first = OrdinaryAgentEnrollmentApprovalReference(
            principal_id="agent_one", operation_id="request-one"
        )
        second = OrdinaryAgentEnrollmentApprovalReference(
            principal_id="agent_two", operation_id="request-two"
        )
        store.list_pending_approved_ordinary_agent_enrollments.side_effect = [
            (first,),
            (second,),
            (),
        ]
        store.read_approved_ordinary_agent_enrollment.side_effect = RuntimeError(
            "private failed request context"
        )
        state = OrdinaryAgentEnrollmentRecoveryState()
        outcomes = [
            recover_ordinary_agent_enrollments_once(
                record_store=store, state=state, lease_owner="test-worker", limit=1
            )
            for _ in range(3)
        ]
        self.assertEqual([result.processed for result in outcomes], [1, 1, 0])
        self.assertEqual(
            [
                call.kwargs["after"]
                for call in store.list_pending_approved_ordinary_agent_enrollments.call_args_list
            ],
            [None, first, second],
        )
        self.assertIsNone(state.after)
        self.assertNotIn("private failed request context", repr(outcomes))
        store.apply_approved_ordinary_agent_enrollment.assert_not_called()
