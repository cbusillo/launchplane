import json
import unittest

from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env


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


if __name__ == "__main__":
    unittest.main()
