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
    DeploymentHeld,
    MAINTENANCE_UNTIL_ENV,
    require_deployment_available,
    recovery_marker,
)
from control_plane.dokploy import api, source
from control_plane.database_maintenance_host import RecoveryRequired
from tempfile import TemporaryDirectory
from collections.abc import Callable


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
            with self.assertRaises(ValueError):
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
                return_value={
                    "sourceType": "git",
                    "composeType": "docker-compose",
                    "composePath": "./docker-compose.yml",
                    "command": "",
                    "autoDeploy": False,
                    "customGitUrl": "git@github.com:example/control.git",
                    "env": MAINTENANCE_UNTIL_ENV
                    + "="
                    + deadline
                    + "\nDOCKER_IMAGE_REFERENCE=synthetic:current",
                },
            ),
            patch.object(api, "update_dokploy_target_env") as update,
            patch.object(api, "trigger_deployment") as deploy,
        ):
            with self.assertRaises(DeploymentHeld):
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
                return_value={
                    "sourceType": "git",
                    "composeType": "docker-compose",
                    "composePath": "./docker-compose.yml",
                    "command": "",
                    "autoDeploy": False,
                    "customGitUrl": "git@github.com:example/control.git",
                    "env": MAINTENANCE_UNTIL_ENV
                    + "="
                    + deadline
                    + "\nDOCKER_IMAGE_REFERENCE=synthetic:current",
                },
            ),
            patch.object(api, "update_dokploy_target_env") as update,
            patch.object(api, "trigger_deployment") as deploy,
        ):
            with self.assertRaises(DeploymentHeld):
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
            maintenance.job_directory = Path(temporary)
            maintenance.hold_id = "own"
            with self.assertRaises(RuntimeError):
                with maintenance.fence(deadline):
                    credentials.side_effect = RuntimeError("database failed")
                    self.assertEqual(
                        api.parse_dokploy_env_text(target["env"])[MAINTENANCE_UNTIL_ENV], deadline
                    )
                    raise RuntimeError("database failed")
            maintenance.marker(deadline, remove=True)
            self.assertNotIn(MAINTENANCE_UNTIL_ENV, api.parse_dokploy_env_text(target["env"]))
            self.assertEqual(api.parse_dokploy_env_text(target["env"])["UNRELATED"], "preserve")
            credentials.assert_called_once()
            with self.assertRaises(TimeoutError):
                with maintenance.fence(deadline):
                    maintenance.hold_recovery(deadline, "own")
                    (Path(temporary) / "commit-pending.json").write_text("{}")
                    raise TimeoutError("committed outcome unavailable")
            self.assertEqual(
                api.parse_dokploy_env_text(target["env"])[MAINTENANCE_UNTIL_ENV],
                recovery_marker("own"),
            )
            with self.assertRaises(ValueError):
                require_deployment_available(api.parse_dokploy_env_text(target["env"]))

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
                with self.assertRaises(ValueError):
                    maintenance.observe(deadline=None)


class MarkerTransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.target: dict[str, Any] = {
            "sourceType": "git",
            "composeType": "docker-compose",
            "composePath": "./docker-compose.yml",
            "command": "",
            "autoDeploy": False,
            "customGitUrl": "git@github.com:example/control.git",
            "env": "LAUNCHPLANE_DATABASE_URL=postgresql://own\nDOCKER_IMAGE_REFERENCE=synthetic:current\nUNRELATED=preserve",
        }
        self.posts = 0
        self.post_outcome = "success"
        self.read_unavailable = False

        def fetch(**_values: Any) -> dict[str, Any]:
            if self.read_unavailable:
                raise ConnectionError("provider read unavailable")
            return dict(self.target)

        def request(**values: Any) -> dict[str, Any]:
            self.posts += 1
            transition = json.loads((self.root / "marker-transition.json").read_text())
            self.assertEqual(transition["phase"], "dispatching")
            self.assertEqual(transition["hold_id"], "own")
            self.assertEqual(transition["target_id"], "own")
            if self.post_outcome == "timeout_before_effect":
                raise TimeoutError("provider outcome unknown")
            self.target["env"] = values["payload"]["env"]
            if self.post_outcome == "timeout_after_effect":
                raise TimeoutError("provider response unknown")
            if self.post_outcome == "readback_unavailable":
                self.read_unavailable = True
            return {}

        self.enterContext(
            patch.object(
                source,
                "read_dokploy_config",
                return_value=("https://provider.invalid", "synthetic"),
            )
        )
        self.enterContext(patch.object(api, "fetch_dokploy_target_payload", side_effect=fetch))
        self.enterContext(patch.object(api, "_dokploy_request", side_effect=request))
        self.enterContext(patch.object(api, "list_deployments_for_target", return_value=[]))
        self.enterContext(patch.object(DeploymentMaintenance, "require_idle_workflows"))
        self.maintenance = DeploymentMaintenance(
            target_id="own",
            database_url="postgresql://own",
            image="synthetic:current",
            root=Path("."),
        )
        self.maintenance.job_directory = self.root
        self.maintenance.hold_id = "own"
        self.deadline = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()

    def test_every_transition_is_journaled_and_only_host_removes_the_marker(self) -> None:
        (self.root / "resumed").touch()
        with patch.object(api, "dokploy_request", wraps=api.dokploy_request) as request:
            with patch.object(DeploymentMaintenance, "observe"):
                with self.maintenance.fence(self.deadline):
                    self.assertEqual(self.posts, 1)
            self.assertEqual(self.posts, 1)
            self.maintenance.hold_recovery(self.deadline, "own")
            self.maintenance.marker(recovery_marker("own"), remove=True)
        self.assertEqual(request.call_count, self.posts)
        self.assertEqual(self.posts, 3)
        self.assertNotIn(MAINTENANCE_UNTIL_ENV, api.parse_dokploy_env_text(self.target["env"]))
        self.assertEqual(api.parse_dokploy_env_text(self.target["env"])["UNRELATED"], "preserve")
        self.assertEqual(
            json.loads((self.root / "marker-transition.json").read_text())["phase"], "observed"
        )

    def test_prewrite_read_failure_remains_retryable_without_dispatch(self) -> None:
        self.read_unavailable = True
        with self.assertRaises(ConnectionError):
            self.maintenance.marker(self.deadline)
        self.assertEqual(self.posts, 0)
        self.read_unavailable = False
        self.maintenance.marker(self.deadline)
        self.assertEqual(self.posts, 1)

    def test_uncertain_install_recovery_and_remove_only_reconcile_the_original_effect(self) -> None:
        for action in ("install", "recovery", "remove"):
            with self.subTest(action=action):
                (self.root / "marker-transition.json").unlink(missing_ok=True)
                self.target["env"] = (
                    "LAUNCHPLANE_DATABASE_URL=postgresql://own\nDOCKER_IMAGE_REFERENCE=synthetic:current"
                )
                desired: str | None = self.deadline
                if action != "install":
                    self.target["env"] += "\n" + MAINTENANCE_UNTIL_ENV + "=" + self.deadline
                if action == "recovery":
                    desired = recovery_marker("own")
                elif action == "remove":
                    desired = None
                self.posts = 0
                self.post_outcome = "timeout_before_effect"

                def change() -> None:
                    if action == "recovery":
                        self.maintenance.hold_recovery(self.deadline, "own")
                    else:
                        self.maintenance.marker(self.deadline, remove=action == "remove")

                with self.assertRaises(RecoveryRequired):
                    change()
                self.post_outcome = "success"
                with self.assertRaises(RecoveryRequired):
                    change()
                with self.assertRaises(RecoveryRequired):
                    self.maintenance.marker(self.deadline, remove=True)
                self.assertEqual(self.posts, 1)
                self.target["env"] = api.render_dokploy_env_text_with_overrides(
                    self.target["env"],
                    updates={} if desired is None else {MAINTENANCE_UNTIL_ENV: desired},
                    removals=(MAINTENANCE_UNTIL_ENV,) if desired is None else (),
                    target_type="compose",
                )
                change()
                self.assertEqual(self.posts, 1)

    def test_confirmed_dispatch_with_failed_readback_is_never_dispatched_again(self) -> None:
        self.post_outcome = "readback_unavailable"
        with self.assertRaises(RecoveryRequired):
            self.maintenance.marker(self.deadline)
        self.assertEqual(
            json.loads((self.root / "marker-transition.json").read_text())["phase"], "confirmed"
        )
        self.read_unavailable = False
        self.post_outcome = "success"
        self.maintenance.marker(self.deadline)
        self.assertEqual(self.posts, 1)

    def test_lost_response_after_the_effect_is_observed_without_another_post(self) -> None:
        self.post_outcome = "timeout_after_effect"
        self.maintenance.marker(self.deadline)
        self.assertEqual(self.posts, 1)
        self.assertEqual(
            json.loads((self.root / "marker-transition.json").read_text())["phase"], "observed"
        )

    def test_marker_dispatch_preserves_an_existing_provider_observer(self) -> None:
        effects = []

        def observe(
            _host: str, _path: str, payload: api.JsonObject, send: Callable[[], api.JsonValue]
        ) -> api.JsonValue:
            effects.append(payload["composeId"])
            return send()

        with api.observe_dokploy_mutations(observe):
            self.maintenance.marker(self.deadline)
        self.assertEqual(effects, ["own"])

    def test_a_different_hold_cannot_reconcile_or_replace_a_transition(self) -> None:
        (self.root / "marker-transition.json").write_text(
            json.dumps(
                {
                    "hold_id": "another",
                    "target_id": "own",
                    "action": "install",
                    "before": None,
                    "desired": self.deadline,
                    "phase": "dispatching",
                }
            )
        )
        with self.assertRaises(RecoveryRequired):
            self.maintenance.marker(self.deadline)
        self.assertEqual(self.posts, 0)
