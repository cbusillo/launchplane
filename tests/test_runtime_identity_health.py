import unittest

from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.workflows.runtime_identity_health import (
    HealthcheckPass,
    healthcheck_evidence_with_runtime_identity,
    wait_for_healthcheck_with_retry,
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

    def test_wait_for_healthcheck_calls_keyword_only_probe(self) -> None:
        seen_calls: list[tuple[str, int]] = []

        def wait_once(*, url: str, timeout_seconds: int) -> HealthcheckPass:
            seen_calls.append((url, timeout_seconds))
            return HealthcheckPass(payload={"status": "ok"})

        result = wait_for_healthcheck_with_retry(
            url="https://example.com/health",
            timeout_seconds=30,
            sleep=lambda _seconds: None,
            monotonic=lambda: 0,
            wait_once=wait_once,
        )

        self.assertEqual(result.payload, {"status": "ok"})
        self.assertEqual(seen_calls, [("https://example.com/health", 30)])

    def test_wait_for_healthcheck_retries_unparseable_json(self) -> None:
        attempts = iter(
            (
                HealthcheckPass(json_parse_failed=True),
                HealthcheckPass(payload={"status": "ok"}),
            )
        )
        sleeps: list[float] = []

        result = wait_for_healthcheck_with_retry(
            url="https://example.com/health",
            timeout_seconds=30,
            sleep=sleeps.append,
            monotonic=lambda: 0,
            wait_once=lambda **_kwargs: next(attempts),
        )

        self.assertEqual(result.payload, {"status": "ok"})
        self.assertEqual(sleeps, [1])

    def test_wait_for_runtime_identity_healthcheck_retries_until_identity_parseable(
        self,
    ) -> None:
        identity = RuntimeIdentity(
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            instance="testing",
            deployment_record_id="deployment-123",
            artifact_id="artifact-a",
            source_git_ref="abc123",
        )
        attempts = iter(
            (
                HealthcheckPass(payload={"status": "ok"}),
                HealthcheckPass(payload={"runtime_identity": {"deployment_record_id": "deployment-123"}}),
                HealthcheckPass(payload={"runtime_identity": identity.model_dump(mode="json")}),
            )
        )
        sleeps: list[float] = []

        from control_plane.workflows.runtime_identity_health import (
            wait_for_runtime_identity_healthcheck_with_retry,
        )

        result = wait_for_runtime_identity_healthcheck_with_retry(
            url="https://example.com/health",
            timeout_seconds=30,
            expected_runtime_identity=identity,
            sleep=sleeps.append,
            monotonic=lambda: 0,
            wait_once=lambda **_kwargs: next(attempts),
        )

        self.assertEqual(result.payload, {"runtime_identity": identity.model_dump(mode="json")})
        self.assertEqual(sleeps, [1, 1])


if __name__ == "__main__":
    unittest.main()
