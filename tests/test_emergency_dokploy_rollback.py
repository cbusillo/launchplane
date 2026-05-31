import os
from pathlib import Path
import runpy
import unittest
from unittest.mock import patch


_SCRIPT_PATH = Path("scripts/deploy/emergency-dokploy-rollback.py")


def _load_script() -> type:
    module_globals = runpy.run_path(str(_SCRIPT_PATH), run_name="emergency_dokploy_rollback")
    return type("EmergencyRollbackScript", (), module_globals)


class EmergencyDokployRollbackTests(unittest.TestCase):
    def test_fails_closed_without_allow_env(self) -> None:
        script_module = _load_script()

        with patch.dict(os.environ, {}, clear=True), self.assertRaises(SystemExit) as context:
            script_module.run_break_glass_rollback()

        self.assertIn("LAUNCHPLANE_ALLOW_DIRECT_DOKPLOY_FALLBACK=true", str(context.exception))

    def test_writes_reviewable_evidence(self) -> None:
        script_module = _load_script()
        env = {
            **os.environ,
            "LAUNCHPLANE_ALLOW_DIRECT_DOKPLOY_FALLBACK": "true",
            "LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST": "https://dokploy.example.test",
            "LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN": "token-1",
            "LAUNCHPLANE_DOKPLOY_TARGET_TYPE": "compose",
            "LAUNCHPLANE_DOKPLOY_TARGET_ID": "target-1",
            "LAUNCHPLANE_ROLLBACK_IMAGE_REFERENCE": "ghcr.io/example/launchplane@sha256:old",
            "LAUNCHPLANE_ROLLBACK_REQUEST_STATUS": "503",
            "LAUNCHPLANE_DOKPLOY_DEPLOY_TIMEOUT_SECONDS": "12",
        }

        with patch.dict(os.environ, env, clear=True), patch.object(
            script_module.control_plane_dokploy,
            "fetch_dokploy_target_payload",
            return_value={"env": "DOCKER_IMAGE_REFERENCE=ghcr.io/example/launchplane@sha256:new\n"},
        ) as fetch_target, patch.object(
            script_module.control_plane_dokploy,
            "render_dokploy_env_text_with_overrides",
            return_value="DOCKER_IMAGE_REFERENCE=ghcr.io/example/launchplane@sha256:old\n",
        ) as render_env, patch.object(
            script_module.control_plane_dokploy,
            "update_dokploy_target_env",
        ) as update_env, patch.object(
            script_module.control_plane_dokploy,
            "latest_deployment_for_target",
            return_value={"deploymentId": "deploy-before"},
        ), patch.object(
            script_module.control_plane_dokploy,
            "deployment_key",
            return_value="deploy-before",
        ), patch.object(
            script_module.control_plane_dokploy,
            "trigger_deployment",
        ) as trigger_deployment, patch.object(
            script_module.control_plane_dokploy,
            "wait_for_target_deployment",
            return_value={"status": "done", "deploymentId": "deploy-after"},
        ) as wait_for_deployment:
            evidence = script_module.run_break_glass_rollback()

        self.assertEqual(evidence["schema_version"], 1)
        self.assertEqual(evidence["event"], "launchplane_break_glass_rollback")
        self.assertEqual(evidence["fallback_kind"], "direct_dokploy")
        self.assertEqual(evidence["service_rollback_http_status"], "503")
        self.assertEqual(evidence["target_type"], "compose")
        self.assertEqual(evidence["target_id"], "target-1")
        self.assertTrue(evidence["image_reference_changed"])
        fetch_target.assert_called_once_with(
            host="https://dokploy.example.test",
            token="token-1",
            target_type="compose",
            target_id="target-1",
        )
        render_env.assert_called_once_with(
            "DOCKER_IMAGE_REFERENCE=ghcr.io/example/launchplane@sha256:new\n",
            updates={"DOCKER_IMAGE_REFERENCE": "ghcr.io/example/launchplane@sha256:old"},
        )
        update_env.assert_called_once()
        trigger_deployment.assert_called_once_with(
            host="https://dokploy.example.test",
            token="token-1",
            target_type="compose",
            target_id="target-1",
            no_cache=True,
        )
        wait_for_deployment.assert_called_once_with(
            host="https://dokploy.example.test",
            token="token-1",
            target_type="compose",
            target_id="target-1",
            before_key="deploy-before",
            timeout_seconds=12,
        )


if __name__ == "__main__":
    unittest.main()
