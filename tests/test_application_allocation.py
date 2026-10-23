import json
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from control_plane.application_state_volume import ApplicationStateVolumeAdapter
from control_plane.contracts.idempotency_record import (
    format_launchplane_mutation_timestamp,
    parse_launchplane_mutation_timestamp,
)
from control_plane.workflows.dokploy_target_adoption import (
    APPLICATION_CREATE_PATH,
    ENVIRONMENT_CREATE_PATH,
    PROJECT_CREATE_PATH,
)
from tests.test_application_state_volume import _ApplicationStateVolumeFixture


class ApplicationAllocationTests(_ApplicationStateVolumeFixture):
    def setUp(self) -> None:
        super().setUp()
        self.payload["state_volume_path"] = ""

    def expire_execution(self) -> Any:
        row = self.store.list_held_provider_target_reservations()[0]
        now = format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(
                row.lease_expires_at, field_name="lease_expires_at"
            )
            + timedelta(seconds=1)
        )
        return patch.object(self.store, "_database_mutation_timestamp", return_value=now)

    async def recover_after_acknowledged_phase(self, field: str) -> None:
        checkpoint = ApplicationStateVolumeAdapter._checkpoint_phase
        key = "ordinary-" + field

        def interrupt(adapter: ApplicationStateVolumeAdapter, lease: Any, phase: str) -> None:
            checkpoint(adapter, lease, phase)
            data = json.loads(phase)
            if field in data and "pending" not in data:
                raise OSError("fixture: stopped after acknowledged phase")

        with patch.object(ApplicationStateVolumeAdapter, "_checkpoint_phase", new=interrupt):
            result = await self.post(idempotency_key=key)
        self.assertEqual(result.status_code, 409, result.text)
        self.assertTrue(self.store.list_held_provider_target_reservations(), result.text)
        readback = await self.post(mode="readback", idempotency_key=key)
        self.assertEqual(readback.status_code, 202, readback.text)
        self.assertTrue(json.loads(readback.json()["result"]["provider_effect_phase"])[field])
        with self.expire_execution():
            recovered = await self.post(idempotency_key=key)
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(self.current_testing_target_id(), "created-application")
        self.assertEqual(
            [call.args[2] for call in self.provider.call_args_list],
            [PROJECT_CREATE_PATH, ENVIRONMENT_CREATE_PATH, APPLICATION_CREATE_PATH],
        )
        calls = self.provider.call_args_list.copy()
        replay = await self.post(idempotency_key=key)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(self.provider.call_args_list, calls)
        self.assert_production_unchanged()

    async def test_acknowledged_project_is_durable_and_original_key_recovers(self) -> None:
        await self.recover_after_acknowledged_phase("project")

    async def test_acknowledged_environment_is_durable_and_original_key_recovers(self) -> None:
        await self.recover_after_acknowledged_phase("environment")

    async def test_acknowledged_application_is_durable_and_original_key_recovers(self) -> None:
        await self.recover_after_acknowledged_phase("application")

    async def recover_lost_response(self, path: str) -> None:
        self.fail_after = path
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertTrue(self.store.list_held_provider_target_reservations())
        self.fail_after = ""
        with self.expire_execution():
            recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(
            [call.args[2] for call in self.provider.call_args_list],
            [PROJECT_CREATE_PATH, ENVIRONMENT_CREATE_PATH, APPLICATION_CREATE_PATH],
        )
        self.assert_production_unchanged()

    async def test_lost_project_response_recovers_only_its_unique_marker(self) -> None:
        await self.recover_lost_response(PROJECT_CREATE_PATH)

    async def test_lost_environment_response_recovers_without_creating_another(self) -> None:
        await self.recover_lost_response(ENVIRONMENT_CREATE_PATH)

    async def test_lost_application_response_recovers_without_creating_another(self) -> None:
        await self.recover_lost_response(APPLICATION_CREATE_PATH)

    async def test_unknown_effect_blocks_fresh_keys_and_requires_reviewed_retention(self) -> None:
        self.fail_before = ENVIRONMENT_CREATE_PATH
        uncertain = await self.post()
        self.assertEqual(uncertain.status_code, 409, uncertain.text)
        effects = self.provider.call_args_list.copy()
        readback = await self.post(mode="readback")
        self.assertEqual(readback.status_code, 202, readback.text)
        custody = readback.json()["result"]["custody_sha256"]
        fresh = await self.post(idempotency_key="fresh-while-held")
        self.assertEqual(fresh.status_code, 400, fresh.text)
        changed = await self.post(target_name="different-allocation")
        self.assertEqual(changed.status_code, 400, changed.text)
        with self.expire_execution():
            retry = await self.post()
            self.assertEqual(retry.status_code, 409, retry.text)
            stale = await self.post(
                retain_allocation=True, expected_allocation_custody_sha256="0" * 64
            )
            self.assertEqual(stale.status_code, 409, stale.text)
            retained = await self.post(
                retain_allocation=True, expected_allocation_custody_sha256=custody
            )
            self.assertEqual(retained.status_code, 202, retained.text)
        self.assertEqual(retained.json()["result"]["allocation_state"], "retained_uncertain")
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertFalse(replay.json()["result"]["applied"])
        self.assertEqual(self.provider.call_args_list, effects)
        self.assert_production_unchanged()

    async def test_owner_drift_preserves_acknowledged_project_and_refuses_next_effect(self) -> None:
        mutate = self.provider.side_effect

        def change_owner(
            host: str, token: str, path: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            result: dict[str, Any] = mutate(host, token, path, payload)
            self.store.write_product_profile_record(
                self.profile.model_copy(
                    update={"product": "other-product", "repository": "example/other-product"}
                )
            )
            return result

        self.provider.side_effect = change_owner
        refused = await self.post()
        self.assertEqual(refused.status_code, 409, refused.text)
        readback = await self.post(mode="readback")
        self.assertEqual(readback.status_code, 202, readback.text)
        phase = json.loads(readback.json()["result"]["provider_effect_phase"])
        self.assertEqual(phase["project"], self.placement["project_id"])
        calls = self.provider.call_args_list.copy()
        with self.expire_execution():
            retry = await self.post()
        self.assertEqual(retry.status_code, 403, retry.text)
        self.assertEqual(self.provider.call_args_list, calls)
        self.assertEqual([call.args[2] for call in calls], [PROJECT_CREATE_PATH])

    async def test_committed_adoption_recovers_receipt_without_provider_access(self) -> None:
        with patch.object(
            ApplicationStateVolumeAdapter,
            "_verify_adopted",
            side_effect=OSError("fixture: receipt interrupted"),
        ):
            uncertain = await self.post()
        self.assertEqual(uncertain.status_code, 409, uncertain.text)
        calls = self.provider.call_args_list.copy()
        with (
            self.expire_execution(),
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                side_effect=AssertionError("Committed receipt requires no provider read"),
            ),
            patch(
                "control_plane.dokploy_inventory.read_dokploy_inventory",
                side_effect=AssertionError("No inventory"),
            ),
        ):
            recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(recovered.json()["result"]["configuration_evidence"], "unavailable")
        self.assertEqual(self.provider.call_args_list, calls)

    async def test_legacy_completed_setup_receipt_replays_without_new_allocation(self) -> None:
        from control_plane.application_state_volume import ROUTE
        from control_plane.http_app import idempotency_request_fingerprint

        completed = await self.post()
        self.assertEqual(completed.status_code, 202, completed.text)
        from control_plane.service_auth import LocalOperatorIdentity
        from control_plane.http_routes.mutation_support import idempotency_scope
        from tests.http_app_test_support import _local_operator_bearer_config

        config = _local_operator_bearer_config()
        scope = idempotency_scope(
            LocalOperatorIdentity(
                subject=config.local_operator_subject,
                token_label=config.local_operator_token_label,
            )
        )
        row = self.store.read_idempotency_record(
            scope=scope, route_path=ROUTE, idempotency_key="first-testing-setup"
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.store.write_idempotency_record(
            row.model_copy(
                update={
                    "request_fingerprint": idempotency_request_fingerprint(
                        route_path=ROUTE, payload=self.payload
                    ),
                    "reconciliation_key": "",
                    "provider_target_key": "",
                    "provider_effect_phase": "",
                    "provider_effect_started_at": "",
                }
            )
        )
        effects = self.provider.call_args_list.copy()
        with patch(
            "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
            side_effect=AssertionError("Receipt replay must not allocate"),
        ):
            replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(self.provider.call_args_list, effects)

    async def recover_service_placement(
        self, existing_environment: bool, *, loses_service_scope: bool = False
    ) -> None:
        from control_plane.application_state_volume import ROUTE
        from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope
        from control_plane.provider_operations import run_durable_provider_operation

        self.placement = {
            "project_id": "created-project",
            "name": "existing-project",
            "environments": [],
        }
        self.payload.update(project_id="created-project")
        if existing_environment:
            self.payload.update(environment_id="created-environment")
            self.placement["environments"].append(
                {
                    "environment_id": "created-environment",
                    "name": "existing-environment",
                    "targets": [],
                }
            )
        envelope = DokployTargetSetupEnvelope.model_validate(self.payload)
        adapter = ApplicationStateVolumeAdapter.prepare(
            self.root,
            self.store,
            envelope,
            "service-fixture",
            "fixture",
            self.profile.product,
            "existing-placement",
        )
        self.fail_after = (
            APPLICATION_CREATE_PATH if existing_environment else ENVIRONMENT_CREATE_PATH
        )

        def apply() -> Any:
            return run_durable_provider_operation(
                store=self.store,
                scope=adapter.scope,
                route_path=ROUTE,
                idempotency_key=adapter.operation_key,
                request_fingerprint=adapter.reconciliation_key(),
                lease_owner="fixture-worker",
                response_trace_id="fixture",
                adapter=adapter,
                original_intent=adapter.intent,
            )

        uncertain = apply()
        self.assertEqual(uncertain.status, "reconcile_required")
        self.fail_after = ""
        if loses_service_scope:
            adapter = ApplicationStateVolumeAdapter.prepare(
                self.root,
                self.store,
                envelope,
                adapter.scope,
                "fixture-current",
                self.profile.product,
                adapter.operation_key,
                first_testing_setup=True,
            )
            calls = self.provider.call_args_list.copy()
            with self.expire_execution():
                refused = apply()
            self.assertEqual(refused.status, "reconcile_required")
            self.assertEqual(self.provider.call_args_list, calls)
            return
        with self.expire_execution():
            completed = apply()
        self.assertEqual(completed.status, "completed")
        self.assertEqual(
            [call.args[2] for call in self.provider.call_args_list],
            [APPLICATION_CREATE_PATH]
            if existing_environment
            else [ENVIRONMENT_CREATE_PATH, APPLICATION_CREATE_PATH],
        )
        self.assertEqual(self.current_testing_target_id(), "created-application")

    async def test_service_setup_recovers_original_existing_project(self) -> None:
        await self.recover_service_placement(False)

    async def test_service_setup_recovers_original_existing_environment(self) -> None:
        await self.recover_service_placement(True)

    async def test_another_caller_scope_cannot_read_or_retain_original_custody(self) -> None:
        from tests.http_app_test_support import _local_operator_bearer_config

        self.fail_before = ENVIRONMENT_CREATE_PATH
        uncertain = await self.post()
        self.assertEqual(uncertain.status_code, 409, uncertain.text)
        effects = self.provider.call_args_list.copy()
        self.app = self.make_app(_local_operator_bearer_config(token_label="other-fixture"))
        readback = await self.post(mode="readback")
        self.assertEqual(readback.status_code, 400, readback.text)
        with self.expire_execution():
            retain = await self.post(
                retain_allocation=True, expected_allocation_custody_sha256="0" * 64
            )
        self.assertEqual(retain.status_code, 400, retain.text)
        self.assertEqual(self.provider.call_args_list, effects)
        self.assertTrue(self.store.list_held_provider_target_reservations())

    async def test_recovery_rechecks_current_first_setup_authority_after_service_scope_loss(
        self,
    ) -> None:
        await self.recover_service_placement(False, loses_service_scope=True)
