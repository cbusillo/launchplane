import unittest
from pathlib import Path
from unittest.mock import patch

from control_plane.workflows.launchplane_self_deploy import (
    LaunchplaneSelfDeployRequest,
    execute_launchplane_self_deploy,
)


class LaunchplaneSelfDeployWorkflowTests(unittest.TestCase):
    _BOOTSTRAP_ENV = (
        "DOCKER_IMAGE_REFERENCE=old\n"
        "LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://launchplane:test@db.internal:5432/launchplane\n"
        "LAUNCHPLANE_MASTER_ENCRYPTION_KEY=test-key\n"
        "LAUNCHPLANE_POLICY_B64=dGVzdA==\n"
        "LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET=old-manager-secret\n"
    )

    def test_execute_updates_target_env_and_triggers_deployment(self) -> None:
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": " Compose ",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "oauth_env": {
                    "LAUNCHPLANE_PUBLIC_URL": "https://launchplane.example",
                    "LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK": "provider-network",
                    "LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET": "new-manager-secret",
                },
                "oauth_env_removals": ("LAUNCHPLANE_NPMPLUS_SECRET",),
                "no_cache": True,
            }
        )

        with (
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value={
                    "env": self._BOOTSTRAP_ENV + "LAUNCHPLANE_NPMPLUS_SECRET=npmplus-secret\n"
                },
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"
            ) as trigger_mock,
        ):
            result = execute_launchplane_self_deploy(
                control_plane_root_path=Path("."),
                request=request,
            )

        self.assertEqual(result.target_type, "compose")
        self.assertEqual(result.target_id, "compose-123")
        self.assertTrue(result.image_reference_changed)
        self.assertEqual(
            result.oauth_env_keys_changed,
            (
                "LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK",
                "LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET",
                "LAUNCHPLANE_PUBLIC_URL",
            ),
        )
        self.assertEqual(result.oauth_env_keys_removed, ("LAUNCHPLANE_NPMPLUS_SECRET",))
        update_env_mock.assert_called_once()
        updated_env_text = update_env_mock.call_args.kwargs["env_text"]
        self.assertIn(
            "DOCKER_IMAGE_REFERENCE=ghcr.io/cbusillo/launchplane@sha256:new",
            updated_env_text,
        )
        self.assertIn("LAUNCHPLANE_PUBLIC_URL=https://launchplane.example", updated_env_text)
        self.assertIn("LAUNCHPLANE_COMPOSE_EXTERNAL_NETWORK=provider-network", updated_env_text)
        self.assertIn(
            "LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET=new-manager-secret",
            updated_env_text,
        )
        self.assertNotIn("LAUNCHPLANE_NPMPLUS_SECRET=", updated_env_text)
        trigger_mock.assert_called_once_with(
            host="https://dokploy.example.com",
            token="token-123",
            target_type="compose",
            target_id="compose-123",
            no_cache=True,
        )

    def test_execute_stops_before_mutation_when_bootstrap_env_missing(self) -> None:
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
            }
        )

        with (
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value={"env": "DOCKER_IMAGE_REFERENCE=old\n"},
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"
            ) as trigger_mock,
        ):
            with self.assertRaisesRegex(ValueError, "Launchplane self deploy target preflight"):
                execute_launchplane_self_deploy(
                    control_plane_root_path=Path("."),
                    request=request,
                )

        update_env_mock.assert_not_called()
        trigger_mock.assert_not_called()

    def test_execute_stops_before_mutation_when_manager_preview_webhook_secret_missing(
        self,
    ) -> None:
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
            }
        )
        target_env = self._BOOTSTRAP_ENV.replace(
            "LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET=old-manager-secret\n",
            "",
        )

        with (
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value={"env": target_env},
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"
            ) as trigger_mock,
        ):
            with self.assertRaisesRegex(
                ValueError,
                "LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET",
            ):
                execute_launchplane_self_deploy(
                    control_plane_root_path=Path("."),
                    request=request,
                )

        update_env_mock.assert_not_called()
        trigger_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
