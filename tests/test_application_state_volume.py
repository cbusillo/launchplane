from copy import deepcopy
import json
from typing import Any
from unittest.mock import patch

from control_plane.application_state_volume import LANE_LOCK_PREFIX, MOUNT_CREATE
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
        self.placement: dict[str, Any] = {}
        self.enterContext(
            patch(
                "control_plane.dokploy_inventory.read_dokploy_inventory", side_effect=self.inventory
            )
        )
        self.fail_after = ""
        self.fail_before = ""
        self.preexisting_mounts: list[dict[str, Any]] = []
        self.application_suffix = "abc123"
        self.provider.side_effect = self.mutate
        self.enterContext(
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                side_effect=lambda *_args: deepcopy(self.live),
            )
        )

    def mutate(self, _host: str, _token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if path == self.fail_before:
            raise TimeoutError("fixture: request outcome unavailable")
        if path == PROJECT_CREATE_PATH:
            self.placement = {
                "project_id": "created-project",
                "name": payload["name"],
                "environments": [],
            }
        if path == ENVIRONMENT_CREATE_PATH:
            self.placement["environments"].append(
                {"environment_id": "created-environment", "name": payload["name"], "targets": []}
            )
        if path == APPLICATION_CREATE_PATH:
            self.live = {
                **payload,
                "appName": payload.get("appName", "ordinary-app") + "-" + self.application_suffix,
                "applicationId": "created-application",
                "serverId": self.payload["server_id"],
                "mounts": deepcopy(self.preexisting_mounts),
                "deployments": [],
            }
            self.placement["environments"][0]["targets"].append(
                {
                    "target_id": self.live["applicationId"],
                    "name": payload["name"],
                    "category": "application",
                    "placement": {
                        "kind": "server",
                        "server_id": self.payload["server_id"],
                        "server_name": "Fixture",
                    },
                }
            )
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
        responses: dict[str, dict[str, Any]] = {
            PROJECT_CREATE_PATH: {
                "project": {"projectId": "created-project"},
                "environment": {"environmentId": "default-environment"},
            },
            ENVIRONMENT_CREATE_PATH: {"environmentId": "created-environment"},
            APPLICATION_CREATE_PATH: {
                "applicationId": "created-application",
                "appName": str(self.live.get("appName", "")),
            },
            MOUNT_CREATE: {"mountId": "created-mount"},
        }
        return responses[path]

    def inventory(self, **_kwargs: Any) -> Any:
        from control_plane.dokploy_inventory import InventoryPage, InventoryProject

        projects = [deepcopy(self.placement)] if self.placement else []
        return InventoryPage(
            inventory_digest="fixture",
            total_projects=len(projects),
            total_environments=sum(len(p["environments"]) for p in projects),
            total_targets=sum(len(e["targets"]) for p in projects for e in p["environments"]),
            offset=0,
            limit=2000,
            next_offset=None,
            projects=[InventoryProject.model_validate(p) for p in projects],
        )

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
        self.assertEqual(
            json.loads(readback.json()["result"]["provider_effect_phase"])["pending"], MOUNT_CREATE
        )

    async def test_plain_setup_cannot_bypass_unresolved_storage_creation(self) -> None:
        await self.plan()
        self.fail_before = MOUNT_CREATE
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        calls = self.provider.call_count
        self.fail_before = ""
        for operation in ("create-application", "create-compose"):
            with self.subTest(operation=operation):
                plain = await self.post(
                    operation=operation,
                    idempotency_key="ordinary-retry-" + operation,
                    state_volume_path="",
                    state_volume_plan_id="",
                    expected_plan_sha256="",
                )
                self.assertIn(plain.status_code, (400, 409), plain.text)
                self.assertEqual(self.provider.call_count, calls)

    async def test_lost_application_response_resumes_only_mount_then_adopts(self) -> None:
        await self.plan()
        self.fail_after = APPLICATION_CREATE_PATH
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        self.fail_after = ""
        calls = self.provider.call_count
        retry = await self.post()
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual([c.args[2] for c in self.provider.call_args_list[calls:]], [MOUNT_CREATE])
        self.assert_production_unchanged()

    async def test_lost_project_response_recovers_marker_without_repeating_project(self) -> None:
        await self.plan()
        self.fail_after = PROJECT_CREATE_PATH
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        self.fail_after = ""
        calls = self.provider.call_count
        retry = await self.post()
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual(
            [c.args[2] for c in self.provider.call_args_list[calls:]],
            [ENVIRONMENT_CREATE_PATH, APPLICATION_CREATE_PATH, MOUNT_CREATE],
        )

    async def test_lost_environment_response_recovers_marker_without_repeating_placement(
        self,
    ) -> None:
        await self.plan()
        self.fail_after = ENVIRONMENT_CREATE_PATH
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        self.fail_after = ""
        calls = self.provider.call_count
        retry = await self.post()
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual(
            [c.args[2] for c in self.provider.call_args_list[calls:]],
            [APPLICATION_CREATE_PATH, MOUNT_CREATE],
        )

    async def test_absent_unknown_project_can_be_retained_then_fresh_plan_applied(self) -> None:
        from datetime import timedelta
        from control_plane.application_state_volume import ROUTE
        from control_plane.contracts.idempotency_record import (
            format_launchplane_mutation_timestamp,
            parse_launchplane_mutation_timestamp,
        )

        original = await self.plan()
        self.fail_before = PROJECT_CREATE_PATH
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        calls = self.provider.call_count
        self.fail_before = ""
        retry = await self.post()
        self.assertEqual(retry.status_code, 409, retry.text)
        self.assertEqual(self.provider.call_count, calls)
        readback = (await self.post(mode="readback")).json()["result"]
        refused = await self.post(
            retain_state_volume_allocation=True,
            expected_state_volume_custody_sha256=readback["custody_sha256"],
        )
        self.assertEqual(refused.status_code, 409, refused.text)
        row = next(
            r for r in self.store.list_held_provider_target_reservations() if r.route_path == ROUTE
        )
        now = format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(
                row.lease_expires_at, field_name="lease_expires_at"
            )
            + timedelta(seconds=1)
        )
        with patch.object(self.store, "_database_mutation_timestamp", return_value=now):
            retained = await self.post(
                retain_state_volume_allocation=True,
                expected_state_volume_custody_sha256=readback["custody_sha256"],
            )
            self.assertEqual(retained.status_code, 202, retained.text)
            self.assertEqual(retained.json()["result"]["allocation_state"], "retained_uncertain")
            replay = await self.post(
                retain_state_volume_allocation=True,
                expected_state_volume_custody_sha256=readback["custody_sha256"],
            )
            self.assertEqual(replay.status_code, 202, replay.text)
            old_apply = await self.post()
            self.assertEqual(old_apply.json()["result"]["allocation_state"], "retained_uncertain")
            self.assertEqual(self.provider.call_count, calls)
            forbidden = await self.post(idempotency_key="retained-plan-cannot-resume")
            self.assertEqual(forbidden.status_code, 409, forbidden.text)
            self.assertEqual(self.provider.call_count, calls)
            fresh = await self.post(
                mode="dry-run", state_volume_plan_id="", expected_plan_sha256=""
            )
            self.assertEqual(fresh.status_code, 202, fresh.text)
            plan = fresh.json()["result"]
            self.assertNotEqual(plan["allocation"], original["allocation"])
            self.assertNotEqual(plan["state_volume"]["name"], original["state_volume"]["name"])
            created = await self.post(
                idempotency_key="fresh-allocation",
                state_volume_plan_id=plan["state_volume_plan_id"],
                expected_plan_sha256=plan["plan_sha256"],
            )
            self.assertEqual(created.status_code, 202, created.text)
        self.assert_production_unchanged()

    async def test_readback_outage_and_missing_target_are_bounded_observations(self) -> None:
        import click
        from http.client import IncompleteRead

        await self.plan()
        result = await self.post()
        self.assertEqual(result.status_code, 202, result.text)
        calls = self.provider.call_count
        for failure in (
            click.ClickException("HTTP 404: private provider details"),
            click.ClickException("connection failed: private provider details"),
            IncompleteRead(b"private provider details", 100),
        ):
            with patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                side_effect=failure,
            ):
                readback = await self.post(mode="readback")
            self.assertEqual(readback.status_code, 202, readback.text)
            self.assertFalse(readback.json()["result"]["configuration_matches"])
            self.assertEqual(readback.json()["result"]["configuration_evidence"], "unavailable")
            self.assertNotIn("private provider details", readback.text)
            self.assertEqual(self.provider.call_count, calls)

    async def test_plain_application_create_works_without_storage_reservation(self) -> None:
        result = await self.post(state_volume_path="")
        self.assertEqual(result.status_code, 202, result.text)
        self.assertEqual(
            [c.args[2] for c in self.provider.call_args_list],
            [PROJECT_CREATE_PATH, ENVIRONMENT_CREATE_PATH, APPLICATION_CREATE_PATH],
        )

    def plain_setup(self, operation: str) -> dict[str, object]:
        from control_plane.dokploy_target_setup_http import execute_dokploy_target_setup

        request = DokployTargetSetupEnvelope.model_validate(
            {
                **self.payload,
                "operation": operation,
                "target_id": "other-target"
                if operation in {"adopt", "complete-compose-source"}
                else "",
                "state_volume_path": "",
                "state_volume_plan_id": "",
                "expected_plan_sha256": "",
            }
        )
        return execute_dokploy_target_setup(
            control_plane_root_path=self.root,
            record_store=self.store,
            request=request,
            required_context_owner=(self.profile.product, self.context),
        )

    def test_plain_adoption_works_without_storage_reservation(self) -> None:
        self.live = {
            "composeId": "other-target",
            "name": "Unused fixture",
            "environmentId": "isolated-environment",
            "serverId": self.payload["server_id"],
            "sourceType": "raw",
        }
        result = self.plain_setup("adopt")
        self.assertTrue(result["applied"])
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            ).target_id,
            "other-target",
        )

    async def test_unresolved_volume_blocks_adoption_and_source_completion(self) -> None:
        await self.plan()
        self.fail_before = PROJECT_CREATE_PATH
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        calls = self.provider.call_count
        for operation in ("adopt", "complete-compose-source"):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                self.plain_setup(operation)
            self.assertEqual(self.provider.call_count, calls)
        with self.assertRaises(FileNotFoundError):
            self.store.read_provider_target_record(
                context_name=self.context, instance_name="testing"
            )

    def test_lane_execution_fence_refuses_all_competing_setup_mutations(self) -> None:
        with self.store.testing_replacement_execution_lock(
            LANE_LOCK_PREFIX + self.context
        ) as check:
            self.assertIsNotNone(check)
            for operation in (
                "create-application",
                "create-compose",
                "adopt",
                "complete-compose-source",
            ):
                with self.subTest(operation=operation), self.assertRaises(ValueError):
                    self.plain_setup(operation)
        self.provider.assert_not_called()

    async def test_unrecorded_next_dispatch_resumes_acknowledged_project(self) -> None:
        from control_plane.application_state_volume_retention import (
            checkpoint_state_volume_progress,
        )

        await self.plan()
        failed = False

        def checkpoint(*args: Any, **kwargs: Any) -> None:
            nonlocal failed
            if json.loads(kwargs["phase"]).get("pending") == ENVIRONMENT_CREATE_PATH and not failed:
                failed = True
                raise OSError("Fixture: interrupted before recording or dispatching next create")
            checkpoint_state_volume_progress(*args, **kwargs)

        with patch(
            "control_plane.application_state_volume_retention.checkpoint_state_volume_progress",
            side_effect=checkpoint,
        ):
            interrupted = await self.post()
        self.assertEqual(interrupted.status_code, 409, interrupted.text)
        calls = self.provider.call_count
        retry = await self.post()
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual(
            [c.args[2] for c in self.provider.call_args_list[calls:]],
            [ENVIRONMENT_CREATE_PATH, APPLICATION_CREATE_PATH, MOUNT_CREATE],
        )

    async def test_duplicate_or_incomplete_recovery_inventory_never_repeats_creation(self) -> None:
        await self.plan()
        self.fail_after = PROJECT_CREATE_PATH
        failed = await self.post()
        self.assertEqual(failed.status_code, 409, failed.text)
        self.fail_after = ""
        calls = self.provider.call_count
        snapshot = self.inventory()
        for bad in (
            snapshot.model_copy(update={"next_offset": 1}),
            snapshot.model_copy(update={"projects": snapshot.projects * 2, "total_projects": 2}),
        ):
            with patch("control_plane.dokploy_inventory.read_dokploy_inventory", return_value=bad):
                retry = await self.post()
            self.assertEqual(retry.status_code, 409, retry.text)
            self.assertEqual(self.provider.call_count, calls)

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
        readback = await self.post(mode="readback")
        phase = json.loads(readback.json()["result"]["provider_effect_phase"])
        self.assertEqual(phase["application"], self.live["applicationId"])
        self.assertEqual(phase["app_name"], self.live["appName"])
        self.assert_production_unchanged()

    async def test_unexpected_generated_name_refuses_and_retains_known_application(self) -> None:
        await self.plan()
        self.application_suffix = "unexpected"
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertNotIn(MOUNT_CREATE, [c.args[2] for c in self.provider.call_args_list])
        readback = await self.post(mode="readback")
        phase = json.loads(readback.json()["result"]["provider_effect_phase"])
        self.assertEqual(phase["application"], self.live["applicationId"])

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

        with self.assertRaises(ValueError):
            execute_dokploy_target_setup(
                control_plane_root_path=self.root,
                record_store=self.store,
                request=DokployTargetSetupEnvelope.model_validate(
                    {**self.payload, "mode": "dry-run"}
                ),
            )
