from __future__ import annotations

import unittest
from unittest.mock import Mock

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentJobClaimRejected,
    OrdinaryAgentJobCursor,
    OrdinaryAgentJobWorkerStore,
)
from control_plane.ordinary_agent_job_worker import OrdinaryAgentJobScanState
from control_plane.ordinary_agent_worker_runtime import (
    OrdinaryAgentWorkerSupportDescriptor,
    OrdinaryAgentWorkerTelemetry,
    run_ordinary_agent_worker_once,
)


class OrdinaryAgentWorkerRuntimeTests(unittest.TestCase):
    def test_ordinary_scan_telemetry_is_independent_from_shared_result(self) -> None:
        store = Mock(spec=OrdinaryAgentJobWorkerStore)
        store.claim_due_ordinary_agent_job.side_effect = OrdinaryAgentJobClaimRejected(
            cursor=OrdinaryAgentJobCursor(request_id="poison-row"),
            reason_code="request_variant_unsupported",
        )
        telemetry = OrdinaryAgentWorkerTelemetry()

        result = run_ordinary_agent_worker_once(
            record_store=store,
            state=OrdinaryAgentJobScanState(),
            telemetry=telemetry,
            worker_id="worker-one",
            lease_seconds=30,
            dispatcher=Mock(),
        )

        self.assertEqual(result.failure_phase, "claim")
        self.assertEqual(telemetry.claim_failures, 1)
        self.assertEqual(telemetry.processed, 0)
        self.assertEqual(telemetry.payload()["last_reason_code"], "request_variant_unsupported")

    def test_support_descriptor_rejects_empty_compatibility_sets(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "no supported finite-request"):
            OrdinaryAgentWorkerSupportDescriptor(finite_request_versions=()).validate()


if __name__ == "__main__":
    unittest.main()
