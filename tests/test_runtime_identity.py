import json
import unittest

from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    compare_runtime_identity,
    health_payload_runtime_identity_status,
    runtime_identity_env,
    runtime_identity_from_health_payload,
)


class RuntimeIdentityTests(unittest.TestCase):
    def test_runtime_identity_env_serializes_non_secret_identity(self) -> None:
        identity = RuntimeIdentity(
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            instance="testing",
            deployment_record_id="deployment-123",
            artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            source_git_ref="abc123",
            image_reference="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            deployed_at="2026-05-09T22:10:00Z",
        )

        env = runtime_identity_env(identity)

        self.assertEqual(env["LAUNCHPLANE_DEPLOYMENT_RECORD_ID"], "deployment-123")
        self.assertEqual(
            env["LAUNCHPLANE_ARTIFACT_ID"],
            "ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
        )
        self.assertEqual(env["LAUNCHPLANE_SOURCE_GIT_REF"], "abc123")
        payload = json.loads(env["LAUNCHPLANE_RUNTIME_IDENTITY_JSON"])
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["product"], "sellyouroutboard")
        self.assertEqual(payload["deployment_record_id"], "deployment-123")

    def test_runtime_identity_from_health_payload_accepts_nested_identity(self) -> None:
        identity = RuntimeIdentity(
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            instance="testing",
            deployment_record_id="deployment-123",
            artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            source_git_ref="abc123",
        )
        payload = {"runtime_identity": identity.model_dump(mode="json")}

        self.assertEqual(runtime_identity_from_health_payload(payload), identity)

    def test_runtime_identity_from_health_payload_accepts_env_json(self) -> None:
        identity = RuntimeIdentity(
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            instance="testing",
            deployment_record_id="deployment-123",
            artifact_id="ghcr.io/cbusillo/sellyouroutboard@sha256:abc123",
            source_git_ref="abc123",
        )

        self.assertEqual(
            runtime_identity_from_health_payload(runtime_identity_env(identity)), identity
        )

    def test_compare_runtime_identity_reports_mismatch_fields(self) -> None:
        expected = RuntimeIdentity(
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            instance="testing",
            deployment_record_id="deployment-123",
            artifact_id="artifact-a",
            source_git_ref="abc123",
        )
        observed = expected.model_copy(
            update={"artifact_id": "artifact-b", "source_git_ref": "def456"}
        )

        status, detail = compare_runtime_identity(expected=expected, observed=observed)

        self.assertEqual(status, "mismatch")
        self.assertIn("artifact_id", detail)
        self.assertIn("source_git_ref", detail)

    def test_compare_runtime_identity_reports_preview_binding_mismatches(self) -> None:
        expected = RuntimeIdentity(
            product="verireel",
            context="verireel-testing",
            instance="pr-310",
            environment_kind="preview",
            deployment_record_id="verireel-testing-pr-310",
            artifact_id="artifact-a",
            source_git_ref="abc123",
            preview_id="preview-verireel-testing-verireel-pr-310",
            preview_generation_id=("preview-verireel-testing-verireel-pr-310-generation-0007"),
        )
        observed = expected.model_copy(
            update={
                "preview_id": "pr-310",
                "preview_generation_id": "",
            }
        )

        status, detail = compare_runtime_identity(expected=expected, observed=observed)

        self.assertEqual(status, "mismatch")
        self.assertIn("preview_id", detail)
        self.assertIn("preview_generation_id", detail)

    def test_health_payload_runtime_identity_status_marks_non_json_unverifiable(self) -> None:
        expected = RuntimeIdentity(
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            instance="testing",
            deployment_record_id="deployment-123",
            artifact_id="artifact-a",
            source_git_ref="abc123",
        )

        status, detail, observed = health_payload_runtime_identity_status(
            expected=expected,
            payload=None,
            json_parse_failed=True,
        )

        self.assertEqual(status, "unverifiable")
        self.assertIn("did not return JSON", detail)
        self.assertIsNone(observed)

    def test_health_payload_runtime_identity_status_marks_invalid_identity_malformed(self) -> None:
        expected = RuntimeIdentity(
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            instance="testing",
            deployment_record_id="deployment-123",
            artifact_id="artifact-a",
            source_git_ref="abc123",
        )

        status, detail, observed = health_payload_runtime_identity_status(
            expected=expected,
            payload={"runtime_identity": {"deployment_record_id": "deployment-123"}},
        )

        self.assertEqual(status, "malformed")
        self.assertIn("could not be parsed", detail)
        self.assertIsNone(observed)


if __name__ == "__main__":
    unittest.main()
