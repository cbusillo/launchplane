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
    async def test_adopted_onboarding_snapshot_refuses_concurrent_name_change(self) -> None:
        from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
        from control_plane.contracts.dokploy_target_record import DokployTargetRecordChanged

        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        target = self.store.read_dokploy_target_record(
            context_name=self.profile.lanes[0].context, instance_name="testing"
        )
        identity = self.store.read_dokploy_target_id_record(
            context_name=target.context, instance_name=target.instance
        )
        concurrent = target.model_copy(update={"target_name": "concurrent-name"})
        self.store.write_dokploy_target_record(concurrent)
        with self.assertRaises(DokployTargetRecordChanged):
            self.store.write_product_authority_bundle(
                ProductAuthorityBundle(
                    dokploy_targets=(target,),
                    expected_dokploy_targets=(target,),
                    expected_dokploy_target_ids=(identity,),
                )
            )
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=target.context, instance_name=target.instance
            ),
            concurrent,
        )

    async def test_ordinary_apply_preserves_creation_response_and_nests_custody(self) -> None:
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        result = response.json()["result"]
        self.assertEqual(result["mode"], "apply")
        self.assertEqual(result["reason"], self.payload["reason"])
        self.assertEqual(result["route_domain_ids"], [])
        setup = result["setup"]
        self.assertEqual(setup["project_id"], "created-project")
        self.assertEqual(setup["environment_id"], "created-environment")
        self.assertEqual(
            setup["plan"]["application"]["target_name"], setup["target_record"]["target_name"]
        )
        self.assertTrue(setup["provider_requests"])
        self.assertEqual(setup["allocation"]["allocation_state"], "adopted")

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
        self.fail_before = ""
        fresh = await self.post(idempotency_key="fresh-after-retention")
        self.assertEqual(fresh.status_code, 202, fresh.text)
        self.assertTrue(fresh.json()["result"]["applied"])
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
        self.assertEqual(
            recovered.json()["result"]["setup"]["allocation"]["configuration_evidence"],
            "unavailable",
        )
        self.assertEqual(self.provider.call_args_list, calls)

    async def recover_receipt_after_owner_change(
        self, *, service: bool, interrupted: bool, storage: bool = False
    ) -> None:
        if service:
            self.set_service_permission(True)
        if storage:
            self.payload["state_volume_path"] = "/var/lib/example"
            await self.plan()
        if interrupted:
            with patch.object(
                ApplicationStateVolumeAdapter,
                "_verify_adopted",
                side_effect=OSError("fixture: interrupted receipt"),
            ):
                initial = await self.post()
            self.assertEqual(initial.status_code, 409, initial.text)
        else:
            initial = await self.post()
            self.assertEqual(initial.status_code, 202, initial.text)
        self.store.write_product_profile_record(self.profile.model_copy(update={"lanes": ()}))
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={"product": "new-owner", "repository": "example/new-owner"}
            )
        )
        calls = self.provider.call_args_list.copy()
        from contextlib import nullcontext

        with (
            self.expire_execution() if interrupted else nullcontext(),
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                side_effect=AssertionError("Receipt recovery requires no provider"),
            ),
            patch(
                "control_plane.dokploy_inventory.read_dokploy_inventory",
                side_effect=AssertionError("Receipt recovery requires no inventory"),
            ),
        ):
            recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertTrue(recovered.json()["result"]["applied"])
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())
        self.assertEqual(self.provider.call_args_list, calls)

    async def test_service_committed_receipt_recovers_after_owner_change(self) -> None:
        await self.recover_receipt_after_owner_change(service=True, interrupted=True)

    async def test_director_committed_receipt_recovers_after_owner_change(self) -> None:
        await self.recover_receipt_after_owner_change(service=False, interrupted=True)

    async def test_completed_receipt_replays_after_owner_change(self) -> None:
        await self.recover_receipt_after_owner_change(service=True, interrupted=False)

    async def test_state_volume_committed_receipt_recovers_after_owner_change(self) -> None:
        await self.recover_receipt_after_owner_change(service=False, interrupted=True, storage=True)

    async def test_incomplete_state_volume_continuation_keeps_original_owner_fence(self) -> None:
        self.payload["state_volume_path"] = "/var/lib/example"
        await self.plan()
        self.fail_after = PROJECT_CREATE_PATH
        uncertain = await self.post()
        self.assertEqual(uncertain.status_code, 409, uncertain.text)
        self.store.write_product_profile_record(self.profile.model_copy(update={"lanes": ()}))
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={"product": "new-owner", "repository": "example/new-owner"}
            )
        )
        calls = self.provider.call_args_list.copy()
        self.fail_after = ""
        with self.expire_execution():
            refused = await self.post()
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertEqual(self.provider.call_args_list, calls)
        self.assertTrue(self.store.list_held_provider_target_reservations())

    async def test_state_volume_completed_receipt_replays_after_owner_change(self) -> None:
        await self.recover_receipt_after_owner_change(
            service=False, interrupted=False, storage=True
        )

    async def recover_environment_with_redundant_project(self, *, interrupted: bool) -> None:
        self.set_service_permission(True)
        self.payload.update(
            project_id="different-project", environment_id="created-environment", project_name=""
        )
        self.placement = {
            "project_id": "created-project",
            "name": "Existing project",
            "environments": [
                {
                    "environment_id": "created-environment",
                    "name": "Existing environment",
                    "targets": [],
                }
            ],
        }
        self.fail_after = APPLICATION_CREATE_PATH if interrupted else ""
        initial = await self.post()
        if interrupted:
            self.assertEqual(initial.status_code, 409, initial.text)
            self.fail_after = ""
            with self.expire_execution():
                result = await self.post()
        else:
            result = initial
        self.assertEqual(result.status_code, 202, result.text)
        self.assertTrue(result.json()["result"]["applied"])
        self.assertEqual(
            [call.args[2] for call in self.provider.call_args_list], [APPLICATION_CREATE_PATH]
        )
        self.assertEqual(self.live["environmentId"], self.payload["environment_id"])
        self.assertEqual(self.current_testing_target_id(), "created-application")

    async def test_supplied_environment_keeps_precedence_on_apply(self) -> None:
        await self.recover_environment_with_redundant_project(interrupted=False)

    async def test_supplied_environment_keeps_precedence_on_lost_response(self) -> None:
        await self.recover_environment_with_redundant_project(interrupted=True)

    async def test_missing_provider_configuration_is_a_structured_pre_effect_refusal(self) -> None:
        import click

        with patch(
            "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
            side_effect=click.ClickException("fixture: Dokploy configuration unavailable"),
        ):
            refused = await self.post()
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertEqual(refused.json()["error"]["code"], "invalid_dokploy_target_setup")
        self.provider.assert_not_called()
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

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
        self,
        existing_environment: bool,
        *,
        loses_service_scope: bool = False,
        committed: bool = False,
    ) -> None:
        from control_plane.application_state_volume import ROUTE
        from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope
        from control_plane.provider_operations import run_durable_provider_operation

        self.placement = {
            "project_id": "created-project",
            "name": "existing-project",
            "environments": [],
        }
        self.payload.update(project_id="created-project", project_name="")
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
        mutate = self.provider.side_effect

        def named_project(
            host: str, token: str, path: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            try:
                result: dict[str, Any] = mutate(host, token, path, payload)
            finally:
                if path == APPLICATION_CREATE_PATH:
                    self.live["environment"]["project"]["name"] = "existing-project"
            return result

        self.provider.side_effect = named_project
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

        if committed:
            self.fail_after = ""
            with patch.object(
                ApplicationStateVolumeAdapter,
                "_verify_adopted",
                side_effect=OSError("fixture: interrupted receipt"),
            ):
                uncertain = apply()
        else:
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
            if committed:
                with patch(
                    "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                    side_effect=AssertionError("Receipt recovery requires no provider"),
                ):
                    completed = apply()
            else:
                completed = apply()
        self.assertEqual(completed.status, "adopted" if committed else "completed")
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

    async def test_existing_project_without_name_recovers_committed_receipt(self) -> None:
        await self.recover_service_placement(False, committed=True)

    async def test_existing_environment_without_name_recovers_committed_receipt(self) -> None:
        await self.recover_service_placement(True, committed=True)

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

    async def test_supplied_placement_refusal_before_effects_does_not_hold_lane(self) -> None:
        from control_plane.application_state_volume import ROUTE
        from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope
        from control_plane.provider_operations import run_durable_provider_operation

        self.register_unused_testing_compose()
        envelope = DokployTargetSetupEnvelope.model_validate(
            {**self.payload, "project_id": "existing-project"}
        )
        adapter = ApplicationStateVolumeAdapter.prepare(
            self.root,
            self.store,
            envelope,
            "service-fixture",
            "fixture",
            self.profile.product,
            "pre-effect-refusal",
        )
        with self.assertRaisesRegex(ValueError, "provider target"):
            run_durable_provider_operation(
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
        self.provider.assert_not_called()
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

    def set_service_permission(self, allowed: bool) -> None:
        from control_plane.contracts.authz_policy_record import (
            LaunchplaneAuthzPolicyRecord,
            build_authz_policy_record_id,
        )
        from control_plane.service_auth import LaunchplaneAuthzPolicy, LocalOperatorPolicyRule
        from tests.http_app_test_support import _local_operator_bearer_config

        config = _local_operator_bearer_config()
        policy = LaunchplaneAuthzPolicy(
            local_operators=(
                LocalOperatorPolicyRule(
                    subjects=(config.local_operator_subject,),
                    token_labels=(config.local_operator_token_label,),
                    contexts=("launchplane",),
                    actions=("dokploy_target.setup",),
                ),
            )
            if allowed
            else (),
        )
        replacement = LaunchplaneAuthzPolicyRecord(
            record_id="fixture-service-policy",
            source="test:ordinary-allocation",
            updated_at=self.profile.updated_at,
            policy=policy,
        )
        current = self.store.seed_authz_policy_if_absent(replacement)
        if current.policy != policy:
            replacement = replacement.model_copy(
                update={
                    "revision": current.revision + 1,
                    "record_id": build_authz_policy_record_id(
                        revision=current.revision + 1, policy_sha256=replacement.policy_sha256
                    ),
                }
            )
            self.assertEqual(
                self.store.compare_and_write_authz_policy_record(
                    expected_record=current, replacement_record=replacement
                ).status,
                "written",
            )
        self.app = self.make_app(config, policy)

    async def retain_service_owned_context(self, *, change: str = "unregistered") -> None:
        self.set_service_permission(True)
        if change in {"unregistered", "registration"}:
            self.payload["context"] = "unregistered-context"
        if change in {"shared", "shared-change"}:
            self.store.write_product_profile_record(
                self.profile.model_copy(
                    update={"product": "other-product", "repository": "example/other"}
                )
            )
        from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope

        self.payload["confirmation"] = DokployTargetSetupEnvelope.model_validate(
            self.payload
        ).expected_confirmation
        self.fail_before = ENVIRONMENT_CREATE_PATH
        uncertain = await self.post()
        self.assertEqual(uncertain.status_code, 409, uncertain.text)
        effects = self.provider.call_args_list.copy()
        if change == "registration":
            self.store.write_product_profile_record(
                self.profile.model_copy(
                    update={
                        "product": "new-owner",
                        "repository": "example/new-owner",
                        "lanes": tuple(
                            lane.model_copy(update={"context": self.payload["context"]})
                            for lane in self.profile.lanes
                        ),
                    }
                )
            )
        elif change == "profile-edit":
            self.store.write_product_profile_record(
                self.profile.model_copy(update={"display_name": "Edited label"})
            )
        elif change == "shared-change":
            self.store.write_product_profile_record(
                self.profile.model_copy(
                    update={"product": "third-product", "repository": "example/third"}
                )
            )
        elif change == "exclusive-change":
            self.store.write_product_profile_record(self.profile.model_copy(update={"lanes": ()}))
            self.store.write_product_profile_record(
                self.profile.model_copy(
                    update={"product": "new-owner", "repository": "example/new-owner"}
                )
            )
        elif change == "authority-loss":
            self.set_service_permission(False)
        if change in {"registration", "profile-edit", "exclusive-change", "shared-change"}:
            with self.expire_execution():
                continuation = await self.post()
            self.assertEqual(continuation.status_code, 409, continuation.text)
            self.assertEqual(self.provider.call_args_list, effects)
        readback = await self.post(mode="readback")
        self.assertEqual(readback.status_code, 202, readback.text)
        if change == "unregistered":
            active = await self.post(
                retain_allocation=True,
                expected_allocation_custody_sha256=readback.json()["result"]["custody_sha256"],
            )
            self.assertEqual(active.status_code, 409, active.text)
        with self.expire_execution():
            retained = await self.post(
                retain_allocation=True,
                expected_allocation_custody_sha256=readback.json()["result"]["custody_sha256"],
            )
        if change == "authority-loss":
            self.assertEqual(retained.status_code, 400, retained.text)
            self.assertTrue(self.store.list_held_provider_target_reservations())
        else:
            self.assertEqual(retained.status_code, 202, retained.text)
            self.assertEqual(retained.json()["result"]["allocation_state"], "retained_uncertain")
            self.assertFalse(retained.json()["result"]["applied"])
            self.assertEqual(self.store.list_held_provider_target_reservations(), ())
            replay = await self.post()
            self.assertEqual(replay.status_code, 202, replay.text)
            self.assertFalse(replay.json()["result"]["applied"])
        self.assertEqual(self.provider.call_args_list, effects)

    async def default_server_setup(self, server: str | None) -> None:
        self.set_service_permission(True)
        self.payload["server_id"] = ""
        mutate = self.provider.side_effect

        def selected_server(
            host: str, token: str, path: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            result: dict[str, Any] = mutate(host, token, path, payload)
            if path == APPLICATION_CREATE_PATH:
                self.assertNotIn("serverId", payload)
                self.live["serverId"] = server
            return result

        self.provider.side_effect = selected_server
        completed = await self.post()
        self.assertEqual(completed.status_code, 202, completed.text)
        self.assertTrue(completed.json()["result"]["applied"])
        self.assertEqual(self.current_testing_target_id(), "created-application")

    async def test_ordinary_default_server_may_be_null(self) -> None:
        await self.default_server_setup(None)

    async def test_ordinary_default_server_may_be_empty(self) -> None:
        await self.default_server_setup("")

    async def test_ordinary_default_server_may_be_provider_selected(self) -> None:
        await self.default_server_setup("provider-selected-server")

    async def test_explicit_server_mismatch_holds_without_adoption(self) -> None:
        mutate = self.provider.side_effect

        def different_server(
            host: str, token: str, path: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            result: dict[str, Any] = mutate(host, token, path, payload)
            if path == APPLICATION_CREATE_PATH:
                self.live["serverId"] = "unexpected-server"
            return result

        self.provider.side_effect = different_server
        refused = await self.post()
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertTrue(self.store.list_held_provider_target_reservations())
        with self.assertRaises(FileNotFoundError):
            self.current_testing_target_id()

    async def test_service_retention_after_profile_edit(self) -> None:
        await self.retain_service_owned_context(change="profile-edit")

    async def test_service_retention_after_shared_owner_change(self) -> None:
        await self.retain_service_owned_context(change="shared-change")

    async def test_service_retention_after_exclusive_owner_change(self) -> None:
        await self.retain_service_owned_context(change="exclusive-change")

    async def test_lost_service_permission_cannot_retain_through_first_setup(self) -> None:
        await self.retain_service_owned_context(change="authority-loss")

    async def test_dry_run_with_original_key_previews_effective_apply_names(self) -> None:
        dry = await self.post(mode="dry-run")
        self.assertEqual(dry.status_code, 202, dry.text)
        self.provider.assert_not_called()
        applied = await self.post()
        self.assertEqual(applied.status_code, 202, applied.text)
        preview = dry.json()["result"]["setup"]
        actual = applied.json()["result"]["setup"]
        self.assertEqual(
            preview["target_record"]["project_name"], actual["target_record"]["project_name"]
        )
        self.assertEqual(
            preview["target_record"]["target_name"], actual["target_record"]["target_name"]
        )

    async def test_service_owned_unregistered_context_can_retain_its_partial_allocation(
        self,
    ) -> None:
        await self.retain_service_owned_context()

    async def test_service_owned_shared_context_can_retain_its_partial_allocation(self) -> None:
        await self.retain_service_owned_context(change="shared")

    async def test_service_owned_context_registration_does_not_strand_original_custody(
        self,
    ) -> None:
        await self.retain_service_owned_context(change="registration")

    async def test_dry_run_without_a_key_reports_non_exact_generated_names(self) -> None:
        dry = await self.post(mode="dry-run", idempotency_key="")
        self.assertEqual(dry.status_code, 202, dry.text)
        self.assertEqual(
            dry.json()["result"]["setup"]["allocation_preview"], "requires-original-key"
        )
        self.provider.assert_not_called()
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())
