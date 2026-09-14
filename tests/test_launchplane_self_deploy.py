import json
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

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
    _ORDINARY_WORKER_COMPOSE_TARGET = {
        "sourceType": "git",
        "composeType": "docker-compose",
        "composePath": "./docker-compose.yml",
        "command": "",
    }

    @classmethod
    def _compose_target(cls, env: str) -> dict[str, str]:
        return {**cls._ORDINARY_WORKER_COMPOSE_TARGET, "env": env}

    @staticmethod
    def _canonical_key_ring() -> str:
        return json.dumps(
            {
                "active_key_id": "root-2026-08",
                "keys": {"root-2026-08": Fernet.generate_key().decode("ascii")},
            },
            separators=(",", ":"),
        )

    def test_replicas_change_requires_a_compose_target_and_real_transition(self) -> None:
        base = {
            "target_type": "compose",
            "target_id": "compose-123",
            "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
        }
        with self.assertRaisesRegex(ValueError, "must not be a no-op"):
            LaunchplaneSelfDeployRequest.model_validate(
                {
                    **base,
                    "ordinary_agent_worker_replicas": {"expected": "0", "desired": "0"},
                }
            )
        with self.assertRaisesRegex(ValueError, "only to compose"):
            LaunchplaneSelfDeployRequest.model_validate(
                {
                    **base,
                    "target_type": "application",
                    "ordinary_agent_worker_replicas": {"expected": "absent", "desired": "1"},
                }
            )

    def test_execute_changes_replicas_with_exact_precondition_and_restores_absence(self) -> None:
        forward = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "ordinary_agent_worker_replicas": {"expected": "absent", "desired": "1"},
            }
        )
        rollback = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:old",
                "ordinary_agent_worker_replicas": {"expected": "1", "desired": "absent"},
            }
        )
        with (
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.fetch_dokploy_target_payload",
                side_effect=[
                    {**self._ORDINARY_WORKER_COMPOSE_TARGET, "env": self._BOOTSTRAP_ENV},
                    {
                        **self._ORDINARY_WORKER_COMPOSE_TARGET,
                        "env": self._BOOTSTRAP_ENV
                        + "LAUNCHPLANE_ORDINARY_AGENT_WORKER_REPLICAS=1\n",
                    },
                ],
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment",
            ),
        ):
            forward_result = execute_launchplane_self_deploy(
                control_plane_root_path=Path("."), request=forward
            )
            rollback_result = execute_launchplane_self_deploy(
                control_plane_root_path=Path("."), request=rollback
            )

        self.assertEqual(forward_result.ordinary_agent_worker_replicas_previous, "absent")
        self.assertEqual(forward_result.ordinary_agent_worker_replicas_desired, "1")
        self.assertEqual(rollback_result.ordinary_agent_worker_replicas_previous, "1")
        self.assertEqual(rollback_result.ordinary_agent_worker_replicas_desired, "absent")
        self.assertIn(
            "LAUNCHPLANE_ORDINARY_AGENT_WORKER_REPLICAS=1",
            update_env_mock.call_args_list[0].kwargs["env_text"],
        )
        self.assertNotIn(
            "LAUNCHPLANE_ORDINARY_AGENT_WORKER_REPLICAS=",
            update_env_mock.call_args_list[1].kwargs["env_text"],
        )

    def test_execute_rejects_invalid_or_unexpected_replicas_before_mutation(self) -> None:
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "ordinary_agent_worker_replicas": {"expected": "0", "desired": "1"},
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
                    **self._ORDINARY_WORKER_COMPOSE_TARGET,
                    "env": self._BOOTSTRAP_ENV + "LAUNCHPLANE_ORDINARY_AGENT_WORKER_REPLICAS=2\n",
                },
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"
            ) as trigger_mock,
        ):
            with self.assertRaisesRegex(ValueError, "unsupported state"):
                execute_launchplane_self_deploy(control_plane_root_path=Path("."), request=request)
        update_env_mock.assert_not_called()
        trigger_mock.assert_not_called()

    def test_execute_disables_worker_from_one_to_zero(self) -> None:
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "ordinary_agent_worker_replicas": {"expected": "1", "desired": "0"},
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
                    **self._ORDINARY_WORKER_COMPOSE_TARGET,
                    "env": self._BOOTSTRAP_ENV + "LAUNCHPLANE_ORDINARY_AGENT_WORKER_REPLICAS=1\n",
                },
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch("control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"),
        ):
            result = execute_launchplane_self_deploy(
                control_plane_root_path=Path("."), request=request
            )

        self.assertEqual(result.ordinary_agent_worker_replicas_previous, "1")
        self.assertEqual(result.ordinary_agent_worker_replicas_desired, "0")
        self.assertIn(
            "LAUNCHPLANE_ORDINARY_AGENT_WORKER_REPLICAS=0",
            update_env_mock.call_args.kwargs["env_text"],
        )

    def test_execute_preserves_existing_one_replica_without_updating_target_env(self) -> None:
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "old",
            }
        )
        target_env = self._BOOTSTRAP_ENV.replace(
            "DOCKER_IMAGE_REFERENCE=old\n",
            "DOCKER_IMAGE_REFERENCE=old\nLAUNCHPLANE_ORDINARY_AGENT_WORKER_REPLICAS=1\n",
        )
        with (
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value=self._compose_target(target_env),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"
            ) as trigger_mock,
        ):
            result = execute_launchplane_self_deploy(
                control_plane_root_path=Path("."), request=request
            )

        self.assertEqual(result.ordinary_agent_worker_replicas_previous, "1")
        self.assertEqual(result.ordinary_agent_worker_replicas_desired, "1")
        update_env_mock.assert_not_called()
        trigger_mock.assert_called_once()

    def test_execute_rejects_incompatible_worker_compose_target_before_mutation(self) -> None:
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "ordinary_agent_worker_replicas": {"expected": "absent", "desired": "1"},
            }
        )
        with (
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.fetch_dokploy_target_payload",
            ) as fetch_target_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"
            ) as trigger_mock,
        ):
            for override in (
                {"sourceType": "raw"},
                {"composeType": "stack"},
                {"composeType": ""},
                {"composePath": "./other-compose.yml"},
                {"command": "docker compose up --scale launchplane-ordinary-agent-workers=2"},
                {"env": self._BOOTSTRAP_ENV + "COMPOSE_FILE=other-compose.yml\n"},
            ):
                with self.subTest(override=override):
                    fetch_target_mock.return_value = {
                        **self._compose_target(self._BOOTSTRAP_ENV),
                        **override,
                    }
                    with self.assertRaisesRegex(ValueError, "compose target is incompatible"):
                        execute_launchplane_self_deploy(
                            control_plane_root_path=Path("."), request=request
                        )
                    update_env_mock.assert_not_called()
                    trigger_mock.assert_not_called()

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
                    **self._ORDINARY_WORKER_COMPOSE_TARGET,
                    "env": self._BOOTSTRAP_ENV + "LAUNCHPLANE_NPMPLUS_SECRET=npmplus-secret\n",
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

    def test_request_rejects_invalid_oauth_env_preconditions(self) -> None:
        base_request = {
            "target_type": "compose",
            "target_id": "compose-123",
            "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
        }

        with self.assertRaisesRegex(ValueError, "expected-absent keys must also be updated"):
            LaunchplaneSelfDeployRequest.model_validate(
                {
                    **base_request,
                    "oauth_env_expected_absent": ("LAUNCHPLANE_SECRET_KEYS_JSON",),
                }
            )
        with self.assertRaisesRegex(
            ValueError, "expected-value keys must also be updated or removed"
        ):
            LaunchplaneSelfDeployRequest.model_validate(
                {
                    **base_request,
                    "oauth_env_expected_values": {"LAUNCHPLANE_SECRET_KEYS_JSON": "expected"},
                }
            )
        with self.assertRaisesRegex(ValueError, "does not accept oauth_env_expected_absent"):
            LaunchplaneSelfDeployRequest.model_validate(
                {
                    **base_request,
                    "oauth_env_expected_absent": ("UNSUPPORTED_KEY",),
                }
            )
        with self.assertRaisesRegex(ValueError, "both absent and equal"):
            LaunchplaneSelfDeployRequest.model_validate(
                {
                    **base_request,
                    "oauth_env": {"LAUNCHPLANE_DEPLOYMENT_MARKER": "next"},
                    "oauth_env_expected_absent": ("LAUNCHPLANE_DEPLOYMENT_MARKER",),
                    "oauth_env_expected_values": {"LAUNCHPLANE_DEPLOYMENT_MARKER": "previous"},
                }
            )

    def test_execute_accepts_expected_absent_oauth_env_key(self) -> None:
        canonical_key_ring = self._canonical_key_ring()
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "oauth_env": {
                    "LAUNCHPLANE_DEPLOYMENT_MARKER": "deploy-marker",
                    "LAUNCHPLANE_SECRET_KEYS_JSON": canonical_key_ring,
                },
                "oauth_env_expected_absent": ("LAUNCHPLANE_SECRET_KEYS_JSON",),
            }
        )

        with (
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value=self._compose_target(self._BOOTSTRAP_ENV),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"
            ) as trigger_mock,
        ):
            execute_launchplane_self_deploy(
                control_plane_root_path=Path("."),
                request=request,
            )

        updated_env_text = update_env_mock.call_args.kwargs["env_text"]
        self.assertIn("LAUNCHPLANE_DEPLOYMENT_MARKER=deploy-marker", updated_env_text)
        self.assertIn("LAUNCHPLANE_SECRET_KEYS_JSON=", updated_env_text)
        trigger_mock.assert_called_once()

    def test_execute_rejects_present_expected_absent_key_before_mutation(self) -> None:
        canonical_key_ring = self._canonical_key_ring()
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "oauth_env": {"LAUNCHPLANE_SECRET_KEYS_JSON": canonical_key_ring},
                "oauth_env_expected_absent": ("LAUNCHPLANE_SECRET_KEYS_JSON",),
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
                    **self._ORDINARY_WORKER_COMPOSE_TARGET,
                    "env": self._BOOTSTRAP_ENV
                    + f"LAUNCHPLANE_SECRET_KEYS_JSON={canonical_key_ring}\n",
                },
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"
            ) as trigger_mock,
        ):
            with self.assertRaisesRegex(ValueError, "to be absent"):
                execute_launchplane_self_deploy(
                    control_plane_root_path=Path("."),
                    request=request,
                )

        update_env_mock.assert_not_called()
        trigger_mock.assert_not_called()

    def test_execute_accepts_matching_expected_oauth_env_value_for_update(self) -> None:
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "oauth_env": {"LAUNCHPLANE_DEPLOYMENT_MARKER": "rollback-marker"},
                "oauth_env_expected_values": {"LAUNCHPLANE_DEPLOYMENT_MARKER": "forward-marker"},
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
                    **self._ORDINARY_WORKER_COMPOSE_TARGET,
                    "env": self._BOOTSTRAP_ENV + "LAUNCHPLANE_DEPLOYMENT_MARKER=forward-marker\n",
                },
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"
            ) as trigger_mock,
        ):
            execute_launchplane_self_deploy(
                control_plane_root_path=Path("."),
                request=request,
            )

        updated_env_text = update_env_mock.call_args.kwargs["env_text"]
        self.assertIn("LAUNCHPLANE_DEPLOYMENT_MARKER=rollback-marker", updated_env_text)
        trigger_mock.assert_called_once()

    def test_execute_rejects_mismatched_expected_oauth_env_value_before_mutation(self) -> None:
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "oauth_env_removals": ("LAUNCHPLANE_SECRET_KEYS_JSON",),
                "oauth_env_expected_values": {"LAUNCHPLANE_SECRET_KEYS_JSON": "reviewed"},
            }
        )

        with (
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value=self._compose_target(self._BOOTSTRAP_ENV),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"
            ) as trigger_mock,
        ):
            with self.assertRaisesRegex(ValueError, "reviewed expected value"):
                execute_launchplane_self_deploy(
                    control_plane_root_path=Path("."),
                    request=request,
                )

        update_env_mock.assert_not_called()
        trigger_mock.assert_not_called()

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
                return_value=self._compose_target("DOCKER_IMAGE_REFERENCE=old\n"),
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
                return_value=self._compose_target(target_env),
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

    def test_execute_accepts_canonical_only_bootstrap(self) -> None:
        canonical_key_ring = self._canonical_key_ring()
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "oauth_env": {"LAUNCHPLANE_SECRET_KEYS_JSON": canonical_key_ring},
                "oauth_env_removals": ("LAUNCHPLANE_MASTER_ENCRYPTION_KEY",),
            }
        )

        with (
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value=self._compose_target(self._BOOTSTRAP_ENV),
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

        updated_env_text = update_env_mock.call_args.kwargs["env_text"]
        self.assertIn("LAUNCHPLANE_SECRET_KEYS_JSON=", updated_env_text)
        self.assertNotIn("LAUNCHPLANE_MASTER_ENCRYPTION_KEY=", updated_env_text)
        self.assertEqual(
            result.oauth_env_keys_changed,
            ("LAUNCHPLANE_SECRET_KEYS_JSON",),
        )
        self.assertEqual(
            result.oauth_env_keys_removed,
            ("LAUNCHPLANE_MASTER_ENCRYPTION_KEY",),
        )
        self.assertNotIn(canonical_key_ring, result.model_dump_json())
        trigger_mock.assert_called_once()

    def test_execute_accepts_dual_root_migration_bootstrap(self) -> None:
        canonical_key_ring = self._canonical_key_ring()
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "oauth_env": {"LAUNCHPLANE_SECRET_KEYS_JSON": canonical_key_ring},
            }
        )

        with (
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value=self._compose_target(self._BOOTSTRAP_ENV),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"
            ) as trigger_mock,
        ):
            execute_launchplane_self_deploy(
                control_plane_root_path=Path("."),
                request=request,
            )

        updated_env_text = update_env_mock.call_args.kwargs["env_text"]
        self.assertIn("LAUNCHPLANE_SECRET_KEYS_JSON=", updated_env_text)
        self.assertIn("LAUNCHPLANE_MASTER_ENCRYPTION_KEY=test-key", updated_env_text)
        trigger_mock.assert_called_once()

    def test_execute_rejects_invalid_canonical_bootstrap_before_mutation(self) -> None:
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "oauth_env": {"LAUNCHPLANE_SECRET_KEYS_JSON": "not-json"},
            }
        )

        with (
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value=self._compose_target(self._BOOTSTRAP_ENV),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"
            ) as trigger_mock,
        ):
            with self.assertRaisesRegex(ValueError, "Invalid LAUNCHPLANE_SECRET_KEYS_JSON"):
                execute_launchplane_self_deploy(
                    control_plane_root_path=Path("."),
                    request=request,
                )

        update_env_mock.assert_not_called()
        trigger_mock.assert_not_called()

    def test_execute_rejects_dual_root_mismatch_before_mutation(self) -> None:
        canonical_key_ring = json.dumps(
            {
                "active_key_id": "root-2026-08",
                "keys": {
                    "root-2026-08": Fernet.generate_key().decode("ascii"),
                    "launchplane-master-key": Fernet.generate_key().decode("ascii"),
                },
            },
            separators=(",", ":"),
        )
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "oauth_env": {"LAUNCHPLANE_SECRET_KEYS_JSON": canonical_key_ring},
            }
        )

        with (
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value=self._compose_target(self._BOOTSTRAP_ENV),
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
                "does not match LAUNCHPLANE_MASTER_ENCRYPTION_KEY",
            ):
                execute_launchplane_self_deploy(
                    control_plane_root_path=Path("."),
                    request=request,
                )

        update_env_mock.assert_not_called()
        trigger_mock.assert_not_called()

    def test_execute_rejects_removing_last_secret_root_before_mutation(self) -> None:
        request = LaunchplaneSelfDeployRequest.model_validate(
            {
                "target_type": "compose",
                "target_id": "compose-123",
                "image_reference": "ghcr.io/cbusillo/launchplane@sha256:new",
                "oauth_env_removals": ("LAUNCHPLANE_MASTER_ENCRYPTION_KEY",),
            }
        )

        with (
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_source.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.fetch_dokploy_target_payload",
                return_value=self._compose_target(self._BOOTSTRAP_ENV),
            ),
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.update_dokploy_target_env"
            ) as update_env_mock,
            patch(
                "control_plane.workflows.launchplane_self_deploy.dokploy_api.trigger_deployment"
            ) as trigger_mock,
        ):
            with self.assertRaisesRegex(ValueError, "Launchplane managed secrets require"):
                execute_launchplane_self_deploy(
                    control_plane_root_path=Path("."),
                    request=request,
                )

        update_env_mock.assert_not_called()
        trigger_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
