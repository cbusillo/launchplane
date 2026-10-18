from copy import deepcopy
from typing import Any
from unittest.mock import patch

from control_plane.application_state_volume import MOUNT_CREATE
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope
from control_plane.workflows.dokploy_target_adoption import (
    APPLICATION_CREATE_PATH,
    ENVIRONMENT_CREATE_PATH,
    PROJECT_CREATE_PATH,
)
from tests.test_director_testing_target_creation import _DirectorTestingSetupFixture


class ApplicationStateVolumeTests(_DirectorTestingSetupFixture):
    def setUp(self) -> None:
        super().setUp()
        self.payload.update(operation="create-application", state_volume_path="/var/lib/example")
        self.live: dict[str, Any] = {}
        self.fail_after = ""
        self.fail_before = ""
        self.preexisting_mounts: list[dict[str, Any]] = []
        self.provider.side_effect = self.mutate
        self.enterContext(
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                side_effect=lambda *_args: deepcopy(self.live),
            )
        )

    def mutate(self, _host: str, _token: str, path: str, payload: dict[str, Any]) -> dict[str, str]:
        if path == self.fail_before:
            raise TimeoutError("fixture: request outcome unavailable")
        if path == APPLICATION_CREATE_PATH:
            self.live = {
                **payload,
                "appName": payload["appName"] + "-fixture",
                "applicationId": "created-application",
                "serverId": self.payload["server_id"],
                "mounts": deepcopy(self.preexisting_mounts),
            }
        if path == MOUNT_CREATE:
            self.live["mounts"].append(
                {
                    "mountId": "created-mount",
                    "applicationId": payload["serviceId"],
                    "type": payload["type"],
                    "volumeName": payload["volumeName"],
                    "mountPath": payload["mountPath"],
                }
            )
        if path == self.fail_after:
            raise TimeoutError("fixture: provider effect completed but response lost")
        return {
            PROJECT_CREATE_PATH: {"projectId": "created-project"},
            ENVIRONMENT_CREATE_PATH: {"environmentId": "created-environment"},
            APPLICATION_CREATE_PATH: {
                "applicationId": "created-application",
                "appName": str(self.live.get("appName", "")),
            },
            MOUNT_CREATE: {"mountId": "created-mount"},
        }[path]

    async def plan(self) -> dict[str, Any]:
        result = await self.post(mode="dry-run")
        self.assertEqual(result.status_code, 202, result.text)
        self.provider.assert_not_called()
        plan: dict[str, Any] = result.json()["result"]
        self.payload.update(
            state_volume_plan_id=plan["state_volume_plan_id"],
            expected_plan_sha256=plan["plan_sha256"],
        )
        return plan

    async def test_creates_only_new_testing_volume_then_replays_and_reads_back(self) -> None:
        plan = await self.plan()
        result = await self.post()
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(
            [c.args[2] for c in self.provider.call_args_list],
            [
                PROJECT_CREATE_PATH,
                ENVIRONMENT_CREATE_PATH,
                APPLICATION_CREATE_PATH,
                MOUNT_CREATE,
            ],
        )
        mount = self.live["mounts"][0]
        self.assertEqual(mount["volumeName"], plan["state_volume"]["name"])
        self.assertEqual(mount["mountPath"], self.payload["state_volume_path"])
        self.assertEqual(mount["applicationId"], self.live["applicationId"])
        calls = self.provider.call_count
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)
        readback = await self.post(mode="readback")
        self.assertTrue(readback.json()["result"]["configuration_matches"], readback.text)
        self.assertEqual(self.provider.call_count, calls)
        self.assert_production_unchanged()

    async def test_production_existing_mount_or_volume_and_host_path_inputs_refuse(self) -> None:
        for changes in (
            {"instance": "prod"},
            {"state_volume_path": "/"},
            {"state_volume_path": "/etc/keys"},
            {"state_volume_path": "/data/../prod"},
            {"state_volume_path": "relative"},
            {"state_volume_path": "/data//state"},
            {"volume_name": "production-data"},
            {"host_path": "/host/data"},
            {"mount_id": "production-mount"},
            {"target_id": "production-compose"},
            {"app_name": "existing-app"},
            {"project_id": "production-project"},
        ):
            with self.subTest(changes=changes):
                result = await self.post(mode="dry-run", **changes)
                self.assertEqual(result.status_code, 400, result.text)
                self.assertEqual(result.json()["error"]["code"], "invalid_request")
        self.provider.assert_not_called()
        self.assert_production_unchanged()

    async def test_stale_plan_and_changed_request_refuse_before_effects(self) -> None:
        await self.plan()
        altered = await self.post(state_volume_path="/other")
        self.assertEqual(altered.status_code, 409, altered.text)
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"display_name": "Changed"})
        )
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        self.provider.assert_not_called()

    async def test_apply_requires_service_plan_and_exact_reviewed_digest(self) -> None:
        result = await self.post()
        self.assertEqual(result.status_code, 400, result.text)
        await self.plan()
        result = await self.post(expected_plan_sha256="0" * 64)
        self.assertEqual(result.status_code, 409, result.text)
        self.provider.assert_not_called()

    async def test_lost_mount_response_recovers_original_without_repeating_any_create(self) -> None:
        await self.plan()
        self.fail_after = MOUNT_CREATE
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        calls = self.provider.call_count
        readback = await self.post(mode="readback")
        self.assertTrue(readback.json()["result"]["configuration_matches"], readback.text)
        with self.assertRaises(FileNotFoundError):
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            )
        result = await self.post()
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(self.provider.call_count, calls)
        self.assert_production_unchanged()

    async def test_unknown_before_mount_preserves_target_and_never_creates_duplicates(self) -> None:
        await self.plan()
        self.fail_before = MOUNT_CREATE
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        calls = self.provider.call_count
        self.fail_before = ""
        retry = await self.post()
        self.assertEqual(retry.status_code, 409, retry.text)
        different_key = await self.post(idempotency_key="do-not-duplicate")
        self.assertEqual(different_key.status_code, 409, different_key.text)
        self.assertEqual(self.provider.call_count, calls)
        readback = await self.post(mode="readback")
        self.assertFalse(readback.json()["result"]["configuration_matches"])

    async def test_lost_application_response_retains_known_placement_without_recreating(
        self,
    ) -> None:
        await self.plan()
        self.fail_after = APPLICATION_CREATE_PATH
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        calls = self.provider.call_count
        retry = await self.post()
        self.assertEqual(retry.status_code, 409, retry.text)
        self.assertEqual(self.provider.call_count, calls)
        readback = await self.post(mode="readback")
        phase = readback.json()["result"]["provider_effect_phase"]
        self.assertIn("created-project", phase)
        self.assertIn("created-environment", phase)
        self.assertFalse(readback.json()["result"]["configuration_matches"])

    async def test_next_artifact_update_preserves_the_same_mount(self) -> None:
        from control_plane.workflows.dokploy_deploy import update_dokploy_target_artifact

        await self.plan()
        result = await self.post()
        self.assertEqual(result.status_code, 202, result.text)
        original = deepcopy(self.live["mounts"])

        def provider_update(**kwargs: Any) -> dict[str, Any]:
            self.live.update(kwargs["payload"])
            return deepcopy(self.live)

        with (
            patch(
                "control_plane.dokploy.api.fetch_dokploy_target_payload",
                side_effect=lambda **_kwargs: deepcopy(self.live),
            ),
            patch("control_plane.dokploy.api.dokploy_request", side_effect=provider_update),
        ):
            for artifact in (
                "registry.example.invalid/site:sha-first",
                "registry.example.invalid/site:sha-next",
            ):
                update_dokploy_target_artifact(
                    host="https://provider.example.invalid",
                    token="fixture",
                    target_type="application",
                    target_id=self.live["applicationId"],
                    artifact_id=artifact,
                )
                self.assertEqual(self.live["dockerImage"], artifact)
                self.assertEqual(self.live["mounts"], original)

    async def test_existing_or_unknown_mounts_never_receive_the_new_volume(self) -> None:
        await self.plan()
        self.preexisting_mounts = [{"mountId": "old", "volumeName": "shared-data"}]
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertNotIn(MOUNT_CREATE, [c.args[2] for c in self.provider.call_args_list])
        self.assert_production_unchanged()

    async def test_provider_alias_to_production_refuses_mounting(self) -> None:
        await self.plan()
        original = self.mutate

        def aliased(host: str, token: str, path: str, payload: dict[str, Any]) -> dict[str, str]:
            response = original(host, token, path, payload)
            if path == APPLICATION_CREATE_PATH:
                self.store.write_provider_target_record(
                    ProviderTargetRecord.from_dokploy_records(
                        target_record=self.production.model_copy(
                            update={"target_type": "application"}
                        ),
                        target_id_record=self.production_id.model_copy(
                            update={"target_id": "created-application"}
                        ),
                    )
                )
            return response

        self.provider.side_effect = aliased
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertNotIn(MOUNT_CREATE, [c.args[2] for c in self.provider.call_args_list])

    def test_local_setup_cannot_silently_ignore_storage_request(self) -> None:
        from control_plane.dokploy_target_setup_http import execute_dokploy_target_setup

        with self.assertRaisesRegex(ValueError, "durable service"):
            execute_dokploy_target_setup(
                control_plane_root_path=self.root,
                record_store=self.store,
                request=DokployTargetSetupEnvelope.model_validate(
                    {**self.payload, "mode": "dry-run"}
                ),
            )
