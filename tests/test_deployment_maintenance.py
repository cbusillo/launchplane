from datetime import datetime, timedelta, timezone
import os
import json
from pathlib import Path
import runpy
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.deployment_maintenance import (
    DeploymentMaintenance,
    MAINTENANCE_UNTIL_ENV,
    require_deployment_available,
)
from control_plane.dokploy import api, source


class DeploymentMaintenanceTests(unittest.TestCase):
    def test_only_absent_or_expired_deadlines_allow_a_deployment(self) -> None:
        require_deployment_available({})
        require_deployment_available(
            {MAINTENANCE_UNTIL_ENV: (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()}
        )
        for deadline in (
            "invalid",
            datetime.now().isoformat(),
            (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        ):
            with self.subTest(deadline=deadline), self.assertRaises(ValueError):
                require_deployment_available({MAINTENANCE_UNTIL_ENV: deadline})

    def test_another_database_is_refused_before_provider_mutation(self) -> None:
        target: dict[str, Any] = {
            "sourceType": "git",
            "composeType": "docker-compose",
            "composePath": "./docker-compose.yml",
            "command": "",
            "autoDeploy": False,
            "env": "LAUNCHPLANE_DATABASE_URL=postgresql://other\nDOCKER_IMAGE_REFERENCE=synthetic:current",
            "customGitUrl": "git@github.com:example/control.git",
        }
        with (
            patch.object(
                source,
                "read_dokploy_config",
                return_value=("https://provider.invalid", "synthetic"),
            ),
            patch.object(api, "fetch_dokploy_target_payload", return_value=target),
            patch.object(api, "update_dokploy_target_env") as update,
        ):
            with self.assertRaisesRegex(ValueError, "running Launchplane"):
                DeploymentMaintenance(
                    target_id="synthetic",
                    database_url="postgresql://own",
                    image="synthetic:current",
                    root=Path("."),
                )
        update.assert_not_called()

    def test_emergency_rollback_refuses_hold_before_provider_writes(self) -> None:
        script = runpy.run_path(
            "scripts/deploy/emergency-dokploy-rollback.py", run_name="emergency_test"
        )
        env = {
            "LAUNCHPLANE_ALLOW_DIRECT_DOKPLOY_FALLBACK": "true",
            "LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST": "https://provider.invalid",
            "LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN": "synthetic",
            "LAUNCHPLANE_DOKPLOY_TARGET_TYPE": "compose",
            "LAUNCHPLANE_DOKPLOY_TARGET_ID": "own",
            "LAUNCHPLANE_ROLLBACK_IMAGE_REFERENCE": "synthetic:old",
            "LAUNCHPLANE_ROLLBACK_REASON": "fixture",
            "LAUNCHPLANE_ROLLBACK_REQUEST_STATUS": "503",
        }
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        with (
            patch.dict(os.environ, env, clear=True),
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                return_value={"env": MAINTENANCE_UNTIL_ENV + "=" + deadline},
            ),
            patch.object(api, "update_dokploy_target_env") as update,
            patch.object(api, "trigger_deployment") as deploy,
        ):
            with self.assertRaisesRegex(ValueError, "held for database maintenance"):
                script["run_break_glass_rollback"]()
        update.assert_not_called()
        deploy.assert_not_called()

    def test_normal_self_deploy_refuses_hold_before_provider_writes(self) -> None:
        from control_plane.workflows.launchplane_self_deploy import (
            LaunchplaneSelfDeployRequest,
            execute_launchplane_self_deploy,
        )

        request = LaunchplaneSelfDeployRequest(
            target_type="compose",
            target_id="own",
            image_reference="registry.invalid/image@sha256:abc",
        )
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        with (
            patch.object(
                source,
                "read_dokploy_config",
                return_value=("https://provider.invalid", "synthetic"),
            ),
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                return_value={"env": MAINTENANCE_UNTIL_ENV + "=" + deadline},
            ),
            patch.object(api, "update_dokploy_target_env") as update,
            patch.object(api, "trigger_deployment") as deploy,
        ):
            with self.assertRaisesRegex(ValueError, "held for database maintenance"):
                execute_launchplane_self_deploy(control_plane_root_path=Path("."), request=request)
        update.assert_not_called()
        deploy.assert_not_called()

    def test_cached_provider_cleanup_does_not_need_a_healthy_database(self) -> None:
        from tempfile import TemporaryDirectory

        target: dict[str, Any] = {
            "sourceType": "git",
            "composeType": "docker-compose",
            "composePath": "./docker-compose.yml",
            "command": "",
            "autoDeploy": False,
            "customGitUrl": "git@github.com:example/control.git",
            "env": "LAUNCHPLANE_DATABASE_URL=postgresql://own\nDOCKER_IMAGE_REFERENCE=synthetic:current\nUNRELATED=preserve",
        }

        def update(**values: Any) -> None:
            target["env"] = values["env_text"]

        deadline = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
        with (
            TemporaryDirectory() as temporary,
            patch.object(
                source,
                "read_dokploy_config",
                return_value=("https://provider.invalid", "synthetic"),
            ) as credentials,
            patch.object(
                api, "fetch_dokploy_target_payload", side_effect=lambda **_values: dict(target)
            ),
            patch.object(api, "update_dokploy_target_env", side_effect=update),
            patch.object(api, "list_deployments_for_target", return_value=[]),
            patch.object(api, "dokploy_request", return_value=[]),
            patch.object(DeploymentMaintenance, "require_idle_workflows"),
        ):
            maintenance = DeploymentMaintenance(
                target_id="own",
                database_url="postgresql://own",
                image="synthetic:current",
                root=Path("."),
            )
            receipt = Path(temporary) / "release.json"
            with self.assertRaisesRegex(RuntimeError, "database failed"):
                with maintenance.fence(deadline, release_receipt=receipt):
                    credentials.side_effect = RuntimeError("database failed")
                    self.assertEqual(
                        api.parse_dokploy_env_text(target["env"])[MAINTENANCE_UNTIL_ENV], deadline
                    )
                    raise RuntimeError("database failed")
            self.assertTrue(json.loads(receipt.read_text())["released"])
            self.assertNotIn(MAINTENANCE_UNTIL_ENV, api.parse_dokploy_env_text(target["env"]))
            self.assertEqual(api.parse_dokploy_env_text(target["env"])["UNRELATED"], "preserve")
            credentials.assert_called_once()

    def test_completed_queue_jobs_do_not_refuse_an_idle_target(self) -> None:
        target: dict[str, Any] = {
            "sourceType": "git",
            "composeType": "docker-compose",
            "composePath": "./docker-compose.yml",
            "command": "",
            "autoDeploy": False,
            "customGitUrl": "git@github.com:example/control.git",
            "env": "LAUNCHPLANE_DATABASE_URL=postgresql://own\nDOCKER_IMAGE_REFERENCE=synthetic:current",
        }
        with (
            patch.object(
                source,
                "read_dokploy_config",
                return_value=("https://provider.invalid", "synthetic"),
            ),
            patch.object(api, "fetch_dokploy_target_payload", return_value=target),
            patch.object(api, "list_deployments_for_target", return_value=[]),
            patch.object(
                api,
                "dokploy_request",
                return_value=[
                    {"data": {"composeId": "own"}, "state": "completed"},
                    {"data": {"composeId": "own"}, "state": "failed"},
                ],
            ),
        ):
            maintenance = DeploymentMaintenance(
                target_id="own",
                database_url="postgresql://own",
                image="synthetic:current",
                root=Path("."),
            )
            maintenance.observe(deadline=None)
            with patch.object(
                api,
                "dokploy_request",
                return_value=[{"data": {"composeId": "own"}, "state": "active"}],
            ):
                with self.assertRaisesRegex(ValueError, "queued"):
                    maintenance.observe(deadline=None)
