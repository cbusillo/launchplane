import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.dokploy import DokploySourceOfTruth, DokployTargetDefinition
from control_plane.workflows.verireel_environment import (
    VeriReelStableEnvironmentRequest,
    resolve_verireel_stable_environment,
)


class VeriReelStableEnvironmentTests(unittest.TestCase):
    def test_resolves_stable_environment_from_launchplane_records(self) -> None:
        source_of_truth = DokploySourceOfTruth(
            schema_version=1,
            targets=(
                DokployTargetDefinition(
                    context="verireel",
                    instance="prod",
                    target_name="ver-prod-app",
                    target_type="application",
                    target_id="prod-app-123",
                    domains=("ver-prod.shinycomputers.com",),
                    healthcheck_path="/api/health",
                ),
            ),
        )

        with TemporaryDirectory() as temporary_directory_name:
            with (
                patch(
                    "control_plane.workflows.verireel_environment.control_plane_dokploy.read_control_plane_dokploy_source_of_truth",
                    return_value=source_of_truth,
                ),
                patch(
                    "control_plane.workflows.verireel_environment.control_plane_runtime_environments.resolve_runtime_environment_values",
                    return_value={},
                ),
            ):
                result = resolve_verireel_stable_environment(
                    control_plane_root=Path(temporary_directory_name),
                    request=VeriReelStableEnvironmentRequest(instance="prod"),
                )

        self.assertEqual(result.target_name, "ver-prod-app")
        self.assertEqual(result.target_type, "application")
        self.assertEqual(result.target_id, "prod-app-123")
        self.assertEqual(result.primary_base_url, "https://ver-prod.shinycomputers.com")
        self.assertEqual(result.health_urls, ("https://ver-prod.shinycomputers.com/api/health",))


if __name__ == "__main__":
    unittest.main()
