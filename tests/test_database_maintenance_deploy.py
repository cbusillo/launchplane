from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from unittest.mock import patch

from control_plane.database_maintenance import DatabaseMaintenanceHeld
from control_plane.database_maintenance_deploy import (
    MAINTENANCE_DEPLOY_ENV,
    deployment_window,
    require_deployment_available,
)
from control_plane.dokploy import api
from tests import test_launchplane_self_deploy as self_deploy_tests


class DatabaseMaintenanceDeployTests(unittest.TestCase):
    def test_live_and_invalid_deadlines_refuse_but_expired_deadline_releases(self) -> None:
        now = datetime.now(timezone.utc)
        for deadline in ((now + timedelta(minutes=5)).isoformat(), "invalid"):
            with self.subTest(deadline=deadline), self.assertRaises(DatabaseMaintenanceHeld):
                require_deployment_available({MAINTENANCE_DEPLOY_ENV: deadline})
        require_deployment_available(
            {MAINTENANCE_DEPLOY_ENV: (now - timedelta(seconds=1)).isoformat()}
        )

    def test_release_preserves_other_settings_after_rehearsal_failure(self) -> None:
        payload = {
            **self_deploy_tests.LaunchplaneSelfDeployWorkflowTests._compose_target(
                self_deploy_tests.LaunchplaneSelfDeployWorkflowTests._BOOTSTRAP_ENV
            ),
            "autoDeploy": False,
        }
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()

        def update(**kwargs: object) -> None:
            payload["env"] = str(kwargs["env_text"])

        with (
            patch(
                "control_plane.database_maintenance_deploy.source.read_dokploy_config",
                return_value=("https://fixture.invalid", "synthetic"),
            ),
            patch.object(
                api, "fetch_dokploy_target_payload", side_effect=lambda **_: dict(payload)
            ),
            patch.object(api, "update_dokploy_target_env", side_effect=update),
            patch.object(api, "list_deployments_for_target", return_value=[]),
            patch("control_plane.database_maintenance_deploy.require_idle_deploy_workflow"),
            self.assertRaisesRegex(RuntimeError, "rehearsal"),
        ):
            with deployment_window(
                control_plane_root=Path("."),
                target_id="fixture",
                repository="example/control",
                expires_at=deadline,
            ):
                require_deployment = api.parse_dokploy_env_text(str(payload["env"]))
                with self.assertRaises(DatabaseMaintenanceHeld):
                    require_deployment_available(require_deployment)
                payload["env"] = str(payload["env"]) + "\nUNRELATED=keep\n"
                raise RuntimeError("rehearsal failed")
        environment = api.parse_dokploy_env_text(str(payload["env"]))
        self.assertNotIn(MAINTENANCE_DEPLOY_ENV, environment)
        self.assertEqual(environment["UNRELATED"], "keep")

    def test_emergency_rollback_refuses_before_any_provider_write(self) -> None:
        from tests.test_emergency_dokploy_rollback import _load_script

        script = _load_script()
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        with (
            patch.dict(
                script.run_break_glass_rollback.__globals__,
                {
                    "_require_break_glass_allowed": lambda: None,
                    "_required_env": lambda name: "fixture",
                },
            ),
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                return_value={
                    "env": api.serialize_dokploy_env_text(
                        {MAINTENANCE_DEPLOY_ENV: deadline}, target_type="compose"
                    )
                },
            ),
            patch.object(api, "update_dokploy_target_env") as update,
            patch.object(api, "trigger_deployment") as dispatch,
            self.assertRaises(DatabaseMaintenanceHeld),
        ):
            script.run_break_glass_rollback()
        update.assert_not_called()
        dispatch.assert_not_called()
