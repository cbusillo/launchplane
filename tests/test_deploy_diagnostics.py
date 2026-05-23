import importlib.util
import io
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "deploy"
    / "capture-launchplane-deploy-diagnostics.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "capture_launchplane_deploy_diagnostics", _SCRIPT_PATH
)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - importlib guard
    raise RuntimeError(f"Could not load deploy diagnostics script from {_SCRIPT_PATH}")
deploy_diagnostics = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(deploy_diagnostics)


class CaptureLaunchplaneDeployDiagnosticsTests(unittest.TestCase):
    def test_captures_compose_metadata_and_redacted_container_logs(self) -> None:
        requests: list[dict[str, object]] = []

        def capture_request(**kwargs: object) -> object:
            requests.append(kwargs)
            path = kwargs["path"]
            if path == "/api/compose.one":
                return {
                    "appName": "compose-launchplane-random",
                    "composeStatus": "done",
                    "env": (
                        "DOCKER_IMAGE_REFERENCE=ghcr.io/cbusillo/launchplane@sha256:new\n"
                        "LAUNCHPLANE_DATABASE_URL=postgresql://secret\n"
                    ),
                    "domains": [
                        {
                            "host": "launchplane.example.com",
                            "port": 8080,
                            "path": "/",
                            "serviceName": "launchplane",
                        }
                    ],
                    "deployments": [
                        {
                            "deploymentId": "deploy-1",
                            "status": "done",
                            "title": "latest",
                            "createdAt": "2026-05-23T00:00:00Z",
                        }
                    ],
                }
            if path == "/api/docker.getContainers":
                return [
                    {
                        "containerId": "container-1",
                        "name": "compose-launchplane-random-launchplane-1",
                        "image": "ghcr.io/cbusillo/launchplane",
                        "state": "running",
                        "status": "Up 1 minute",
                        "ports": "8080/tcp",
                    },
                    {"containerId": "other", "name": "postgres-1"},
                ]
            if path == "/api/compose.readLogs":
                return "started\nLAUNCHPLANE_DATABASE_URL=postgresql://secret\n"
            raise AssertionError(f"Unexpected path: {path}")

        output = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST": "https://dokploy.example.com",
                    "LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN": "token-123",
                    "LAUNCHPLANE_DOKPLOY_TARGET_TYPE": "compose",
                    "LAUNCHPLANE_DOKPLOY_TARGET_ID": "compose-123",
                    "LAUNCHPLANE_EXPECTED_IMAGE_REFERENCE": "ghcr.io/cbusillo/launchplane@sha256:new",
                },
                clear=True,
            ),
            patch.object(
                deploy_diagnostics.control_plane_dokploy,
                "dokploy_request",
                side_effect=capture_request,
            ),
            redirect_stdout(output),
        ):
            exit_code = deploy_diagnostics.main()

        self.assertEqual(exit_code, 0)
        self.assertIn('"diagnostic": "dokploy-target"', output.getvalue())
        self.assertIn('"docker_image_matches_expected": true', output.getvalue())
        self.assertIn('"containerId": "container-1"', output.getvalue())
        self.assertIn("LAUNCHPLANE_DATABASE_URL=[redacted]", output.getvalue())
        self.assertNotIn("postgresql://secret", output.getvalue())
        self.assertEqual(requests[-1]["path"], "/api/compose.readLogs")
        self.assertEqual(
            requests[-1]["query"],
            {"composeId": "compose-123", "containerId": "container-1", "tail": 300, "since": "1h"},
        )

    def test_skips_without_break_glass_credentials(self) -> None:
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stdout(output):
            exit_code = deploy_diagnostics.main()

        self.assertEqual(exit_code, 0)
        self.assertIn("Skipping Dokploy diagnostics", output.getvalue())


if __name__ == "__main__":
    unittest.main()
