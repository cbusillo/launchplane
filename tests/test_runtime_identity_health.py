import unittest

from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.workflows.runtime_identity_health import (
    HealthcheckPass,
    healthcheck_evidence_with_runtime_identity,
)


class RuntimeIdentityHealthTests(unittest.TestCase):
    def test_healthcheck_evidence_records_matching_runtime_identity(self) -> None:
        identity = RuntimeIdentity(
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            instance="testing",
            deployment_record_id="deployment-123",
            artifact_id="artifact-a",
            source_git_ref="abc123",
        )

        evidence = healthcheck_evidence_with_runtime_identity(
            HealthcheckEvidence(
                verified=True,
                urls=("https://testing.example/health",),
                timeout_seconds=5,
                status="pass",
            ),
            expected_runtime_identity=identity,
            healthcheck_pass=HealthcheckPass(
                payload={"runtime_identity": identity.model_dump(mode="json")}
            ),
        )

        self.assertEqual(evidence.runtime_identity_status, "match")
        self.assertEqual(evidence.observed_runtime_identity, identity)

    def test_healthcheck_evidence_marks_missing_runtime_identity(self) -> None:
        identity = RuntimeIdentity(
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            instance="testing",
            deployment_record_id="deployment-123",
            artifact_id="artifact-a",
            source_git_ref="abc123",
        )

        evidence = healthcheck_evidence_with_runtime_identity(
            HealthcheckEvidence(
                verified=True,
                urls=("https://testing.example/health",),
                timeout_seconds=5,
                status="pass",
            ),
            expected_runtime_identity=identity,
            healthcheck_pass=HealthcheckPass(payload={"status": "ok"}),
        )

        self.assertEqual(evidence.runtime_identity_status, "missing")
        self.assertIsNone(evidence.observed_runtime_identity)


if __name__ == "__main__":
    unittest.main()
