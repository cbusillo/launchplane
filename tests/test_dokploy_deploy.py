import unittest
from typing import Any
from unittest.mock import patch

from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.workflows.dokploy_deploy import update_dokploy_target_artifact


class DokployDeployTests(unittest.TestCase):
    def test_update_application_injects_runtime_identity_env(self) -> None:
        requests: list[dict[str, Any]] = []

        def _fake_request(**kwargs: object) -> object:
            requests.append(dict(kwargs))
            return {}

        with (
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.fetch_dokploy_target_payload",
                return_value={"env": "EXISTING=true", "username": "u", "password": "p"},
            ),
            patch(
                "control_plane.workflows.dokploy_deploy.control_plane_dokploy.dokploy_request",
                side_effect=_fake_request,
            ),
        ):
            update_dokploy_target_artifact(
                host="https://dokploy.example",
                token="token",
                target_type="application",
                target_id="app-123",
                artifact_id="ghcr.io/example/app@sha256:abc",
                runtime_identity=RuntimeIdentity(
                    product="example",
                    context="example-testing",
                    instance="testing",
                    deployment_record_id="deployment-123",
                    artifact_id="ghcr.io/example/app@sha256:abc",
                    source_git_ref="abc",
                ),
            )

        self.assertEqual(requests[0]["path"], "/api/application.saveEnvironment")
        env_text = str(requests[0]["payload"]["env"])
        self.assertIn("EXISTING=true", env_text)
        self.assertIn("LAUNCHPLANE_DEPLOYMENT_RECORD_ID=deployment-123", env_text)
        self.assertIn("LAUNCHPLANE_ARTIFACT_ID=ghcr.io/example/app@sha256:abc", env_text)
        self.assertEqual(requests[1]["path"], "/api/application.saveDockerProvider")


if __name__ == "__main__":
    unittest.main()
