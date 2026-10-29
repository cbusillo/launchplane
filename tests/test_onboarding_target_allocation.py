import json
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from control_plane.application_state_volume import ApplicationStateVolumeAdapter
from control_plane.contracts.idempotency_record import (
    format_launchplane_mutation_timestamp,
    parse_launchplane_mutation_timestamp,
)
from control_plane.generic_web_onboarding import GenericWebOnboardingIntent
from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope
from control_plane.onboarding_target import onboarding_target_digests
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.service_human_auth import HumanSessionManager, InMemoryHumanSessionStore
from tests.http_app_test_support import (
    _browser_mutation_headers,
    _github_human_identity,
    _github_oauth_config,
    _local_operator_bearer_config,
    _RejectingVerifier,
)
from tests.support.http import request
from tests.support.auth import scoped_agent_transport
from tests.test_application_state_volume import _ApplicationStateVolumeFixture
from control_plane.workflows.dokploy_target_adoption import (
    APPLICATION_CREATE_PATH,
    ENVIRONMENT_CREATE_PATH,
    PROJECT_CREATE_PATH,
)


class OnboardingTargetAllocationTests(_ApplicationStateVolumeFixture):
    def setUp(self) -> None:
        super().setUp()
        self.original_policies = self.store.list_authz_policy_records()
        self.intent = GenericWebOnboardingIntent(
            product="new-site",
            display_name="New Site",
            repository="example/new-site",
            repository_id="123",
            repository_owner_id="456",
            image_repository="ghcr.io/example/new-site",
            runtime_port=3000,
            health_path="/health",
            preview_base_url="https://preview.example.invalid",
            server_id=self.payload["server_id"],
        )
        self.payload.update(
            mode="dry-run",
            state_volume_path="",
            target_type="application",
            context=self.intent.testing_context,
            target_name=self.intent.target_name,
            project_name=self.intent.project_name,
            onboarding=self.intent.model_dump(mode="json"),
        )
        self.manager = HumanSessionManager(
            config=_github_oauth_config(), session_store=InMemoryHumanSessionStore()
        )
        self.session = self.manager.issue(_github_human_identity())
        self.policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_humans": [
                    {
                        "github_ids": [self.session.identity.github_id],
                        "roles": ["admin"],
                        "products": ["launchplane"],
                        "contexts": ["launchplane"],
                        "actions": ["authz_policy_grant.write"],
                    }
                ],
            }
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=self.policy,
            record_store_factory=lambda: self.store,
            human_session_manager=self.manager,
            control_plane_root_path=self.root,
        )

    def assert_production_unchanged(self) -> None:
        super().assert_production_unchanged()
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="prod"
            ),
            self.production_id,
        )
        self.assertEqual(self.store.list_authz_policy_records(), self.original_policies)

    async def post(self, *, idempotency_key: str = "new-site-allocation", **changes: Any) -> Any:
        return await request(
            self.app,
            "POST",
            "/v1/dokploy-targets/setup",
            payload={**self.payload, **changes},
            headers={
                **_browser_mutation_headers(self.manager, self.session),
                "Idempotency-Key": idempotency_key,
            },
        )

    async def plan(self) -> dict[str, Any]:
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        self.provider.assert_not_called()
        result: dict[str, Any] = response.json()["result"]
        self.payload.update(
            mode="apply",
            reviewed_target_request_sha256=result["target_request_sha256"],
            reviewed_onboarding_plan_sha256=result["onboarding_plan_sha256"],
        )
        return result

    async def test_admin_plans_allocates_replays_and_reads_original_without_product_or_policy_apply(
        self,
    ) -> None:
        await self.plan()
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        target = self.store.read_dokploy_target_id_record(
            context_name=self.intent.testing_context, instance_name="testing"
        )
        self.assertEqual(target.target_id, self.live["applicationId"])
        calls = self.provider.call_args_list.copy()
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)
        readback = await self.post(mode="readback")
        self.assertEqual(readback.status_code, 202, readback.text)
        self.assertTrue(readback.json()["result"]["configuration_matches"])
        self.assertEqual(self.provider.call_args_list, calls)
        self.assertEqual(self.store.list_product_profile_records(), (self.profile,))
        self.assert_production_unchanged()

    def expire_execution(self) -> Any:
        row = self.store.list_held_provider_target_reservations()[0]
        now = format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(
                row.lease_expires_at, field_name="lease_expires_at"
            )
            + timedelta(seconds=1)
        )
        return patch.object(self.store, "_database_mutation_timestamp", return_value=now)

    def assert_no_authority_applied(self) -> None:
        self.assertEqual(self.store.list_product_profile_records(), (self.profile,))
        self.assert_production_unchanged()

    async def recover_lost_response(self, path: str) -> None:
        await self.plan()
        self.fail_after = path
        uncertain = await self.post()
        self.assertEqual(uncertain.status_code, 409, uncertain.text)
        readback = await self.post(mode="readback")
        self.assertEqual(readback.status_code, 202, readback.text)
        self.fail_after = ""
        with self.expire_execution():
            recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(
            [call.args[2] for call in self.provider.call_args_list],
            [PROJECT_CREATE_PATH, ENVIRONMENT_CREATE_PATH, APPLICATION_CREATE_PATH],
        )
        self.assert_no_authority_applied()

    async def test_lost_project_response_recovers_without_duplicate_creates(self) -> None:
        await self.recover_lost_response(PROJECT_CREATE_PATH)

    async def test_lost_environment_response_recovers_without_duplicate_creates(self) -> None:
        await self.recover_lost_response(ENVIRONMENT_CREATE_PATH)

    async def test_lost_application_response_recovers_without_duplicate_creates(self) -> None:
        await self.recover_lost_response(APPLICATION_CREATE_PATH)

    async def recover_acknowledged_checkpoint(self, field: str) -> None:
        await self.plan()
        checkpoint = ApplicationStateVolumeAdapter._checkpoint_phase

        def interrupt(adapter: ApplicationStateVolumeAdapter, lease: Any, phase: str) -> None:
            checkpoint(adapter, lease, phase)
            acknowledged = json.loads(phase)
            if field in acknowledged and "pending" not in acknowledged:
                raise OSError("fixture: stopped after acknowledged allocation")

        with patch.object(ApplicationStateVolumeAdapter, "_checkpoint_phase", new=interrupt):
            uncertain = await self.post()
        self.assertEqual(uncertain.status_code, 409, uncertain.text)
        readback = await self.post(mode="readback")
        self.assertEqual(readback.status_code, 202, readback.text)
        self.assertTrue(json.loads(readback.json()["result"]["provider_effect_phase"])[field])
        with self.expire_execution():
            recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(
            [call.args[2] for call in self.provider.call_args_list],
            [PROJECT_CREATE_PATH, ENVIRONMENT_CREATE_PATH, APPLICATION_CREATE_PATH],
        )
        calls = self.provider.call_args_list.copy()
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(self.provider.call_args_list, calls)
        self.assert_no_authority_applied()

    async def test_acknowledged_project_checkpoint_recovers_the_reviewed_plan(self) -> None:
        await self.recover_acknowledged_checkpoint("project")

    async def test_acknowledged_environment_checkpoint_recovers_the_reviewed_plan(self) -> None:
        await self.recover_acknowledged_checkpoint("environment")

    async def test_acknowledged_application_checkpoint_recovers_the_reviewed_plan(self) -> None:
        await self.recover_acknowledged_checkpoint("application")

    async def retain_reviewed_pending_phase(self, path: str, expected_creates: list[str]) -> None:
        await self.plan()
        self.fail_before = path
        uncertain = await self.post()
        self.assertEqual(uncertain.status_code, 409, uncertain.text)
        readback = await self.post(mode="readback")
        self.assertEqual(readback.status_code, 202, readback.text)
        result = readback.json()["result"]
        self.assertEqual(json.loads(result["provider_effect_phase"])["pending"], path)
        calls = self.provider.call_args_list.copy()
        self.assertEqual([call.args[2] for call in calls], expected_creates)
        fresh = await self.post(idempotency_key="different-key")
        self.assertEqual(fresh.status_code, 400, fresh.text)
        with self.expire_execution():
            stale = await self.post(
                retain_allocation=True, expected_allocation_custody_sha256="0" * 64
            )
            self.assertEqual(stale.status_code, 409, stale.text)
            retained = await self.post(
                retain_allocation=True, expected_allocation_custody_sha256=result["custody_sha256"]
            )
        self.assertEqual(retained.status_code, 202, retained.text)
        self.assertEqual(retained.json()["result"]["allocation_state"], "retained_uncertain")
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertFalse(replay.json()["result"]["applied"])
        self.assertEqual(self.provider.call_args_list, calls)
        self.assert_no_authority_applied()

    async def test_reviewed_custody_can_retain_pending_project_allocation(self) -> None:
        await self.retain_reviewed_pending_phase(PROJECT_CREATE_PATH, [PROJECT_CREATE_PATH])

    async def test_reviewed_custody_can_retain_pending_environment_allocation(self) -> None:
        await self.retain_reviewed_pending_phase(
            ENVIRONMENT_CREATE_PATH, [PROJECT_CREATE_PATH, ENVIRONMENT_CREATE_PATH]
        )

    async def test_reviewed_custody_can_retain_pending_application_allocation(self) -> None:
        await self.retain_reviewed_pending_phase(
            APPLICATION_CREATE_PATH,
            [PROJECT_CREATE_PATH, ENVIRONMENT_CREATE_PATH, APPLICATION_CREATE_PATH],
        )

    async def test_changed_reviewed_target_or_record_plan_refuses_before_effects(self) -> None:
        await self.plan()
        for changes in (
            {"description": "changed application"},
            {"reviewed_target_request_sha256": "0" * 64},
            {"reviewed_onboarding_plan_sha256": "0" * 64},
            {"onboarding": {**self.payload["onboarding"], "runtime_port": 8080}},
        ):
            with self.subTest(changes=changes):
                response = await self.post(**changes)
                self.assertEqual(response.status_code, 400, response.text)
                self.provider.assert_not_called()

    async def test_dry_run_requires_a_key_and_changed_plans_need_a_fresh_key(self) -> None:
        missing_key = await self.post(idempotency_key="")
        self.assertEqual(missing_key.status_code, 400, missing_key.text)
        self.assertEqual(missing_key.json()["error"]["code"], "invalid_dokploy_target_setup")
        planned = await self.post()
        self.assertEqual(planned.status_code, 202, planned.text)
        changed = await self.post(description="revised application")
        self.assertEqual(changed.status_code, 400, changed.text)
        fresh = await self.post(idempotency_key="revised-plan", description="revised application")
        self.assertEqual(fresh.status_code, 202, fresh.text)
        self.assertNotEqual(
            fresh.json()["result"]["target_request_sha256"],
            planned.json()["result"]["target_request_sha256"],
        )
        self.provider.assert_not_called()

    async def test_matching_digests_require_the_saved_plan_for_the_original_actor_and_key(
        self,
    ) -> None:
        digests = onboarding_target_digests(DokployTargetSetupEnvelope.model_validate(self.payload))
        response = await self.post(
            mode="apply",
            reviewed_target_request_sha256=digests["target_request_sha256"],
            reviewed_onboarding_plan_sha256=digests["onboarding_plan_sha256"],
        )
        self.assertEqual(response.status_code, 400, response.text)
        await self.plan()
        self.app = self.make_app(_local_operator_bearer_config())
        response = await request(
            self.app,
            "POST",
            "/v1/dokploy-targets/setup",
            payload=self.payload,
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": "new-site-allocation",
            },
        )
        self.assertEqual(response.status_code, 400, response.text)
        self.provider.assert_not_called()

    async def test_context_ownership_drift_after_project_preserves_custody_before_next_create(
        self,
    ) -> None:
        await self.plan()
        mutate = self.provider.side_effect

        def introduce_owner(
            host: str, token: str, path: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            result: dict[str, Any] = mutate(host, token, path, payload)
            self.store.write_product_profile_record(
                self.profile.model_copy(
                    update={"historical_contexts": (self.intent.preview_context,)}
                )
            )
            return result

        self.provider.side_effect = introduce_owner
        response = await self.post()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            [call.args[2] for call in self.provider.call_args_list], [PROJECT_CREATE_PATH]
        )
        readback = await self.post(mode="readback")
        self.assertEqual(readback.status_code, 202, readback.text)
        self.assertEqual(
            json.loads(readback.json()["result"]["provider_effect_phase"])["project"],
            "created-project",
        )

    async def test_existing_or_historical_context_and_existing_product_refuse_before_effects(
        self,
    ) -> None:
        for changes in (
            {"product": self.profile.product},
            {"testing_context": self.context},
            {"preview_context": self.context},
        ):
            intent = self.intent.model_copy(update=changes)
            with self.subTest(changes=changes):
                response = await self.post(
                    onboarding=intent.model_dump(mode="json"), context=intent.testing_context
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.provider.assert_not_called()
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"historical_contexts": (self.intent.preview_context,)})
        )
        response = await self.post()
        self.assertEqual(response.status_code, 400, response.text)
        self.provider.assert_not_called()

    async def test_native_request_cannot_select_existing_placement_source_domains_or_production(
        self,
    ) -> None:
        for changes in (
            {"instance": "prod"},
            {"project_id": "existing-project"},
            {"environment_id": "existing-environment"},
            {"target_id": self.production_id.target_id},
            {"source_type": "git"},
            {"domains": ["production.example.invalid"]},
            {"state_volume_path": "/var/lib/example"},
            {"server_id": ""},
            {"context": self.context},
        ):
            with self.subTest(changes=changes):
                response = await self.post(**changes)
                self.assertEqual(response.status_code, 400, response.text)
                self.provider.assert_not_called()
                self.assert_production_unchanged()

    async def test_browser_requires_csrf_and_a_current_administrator(self) -> None:
        headers = _browser_mutation_headers(self.manager, self.session)
        headers.pop("X-CSRF-Token")
        response = await request(
            self.app, "POST", "/v1/dokploy-targets/setup", payload=self.payload, headers=headers
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: self.store,
            human_session_manager=self.manager,
            control_plane_root_path=self.root,
        )
        response = await self.post()
        self.assertEqual(response.status_code, 403, response.text)
        self.provider.assert_not_called()

    async def test_director_agent_uses_the_same_bounded_plan_and_allocation_path(self) -> None:
        self.app = self.make_app(_local_operator_bearer_config())
        headers = {"Authorization": "Bearer local-operator-token", "Idempotency-Key": "agent-plan"}
        plan = await request(
            self.app, "POST", "/v1/dokploy-targets/setup", payload=self.payload, headers=headers
        )
        self.assertEqual(plan.status_code, 202, plan.text)
        result = plan.json()["result"]
        payload = {
            **self.payload,
            "mode": "apply",
            "reviewed_target_request_sha256": result["target_request_sha256"],
            "reviewed_onboarding_plan_sha256": result["onboarding_plan_sha256"],
        }
        with scoped_agent_transport():
            refused = await request(
                self.app, "POST", "/v1/dokploy-targets/setup", payload=payload, headers=headers
            )
        self.assertEqual(refused.status_code, 403, refused.text)
        self.provider.assert_not_called()
        applied = await request(
            self.app, "POST", "/v1/dokploy-targets/setup", payload=payload, headers=headers
        )
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assert_production_unchanged()

    async def test_allocated_identity_survives_native_record_apply_and_original_key_replay(
        self,
    ) -> None:
        allocation_plan = await self.plan()
        records_payload = {
            "product": "launchplane",
            "mode": "dry_run",
            "generic_web": self.intent.model_dump(mode="json"),
        }

        async def records(**changes: Any) -> Any:
            return await request(
                self.app,
                "POST",
                "/v1/product-onboarding/apply",
                payload={**records_payload, **changes},
                headers={
                    **_browser_mutation_headers(self.manager, self.session),
                    "Idempotency-Key": "native-onboarding-records",
                },
            )

        planned = await records()
        self.assertEqual(planned.status_code, 202, planned.text)
        self.assertEqual(
            planned.json()["result"]["plan_sha256"], allocation_plan["onboarding_plan_sha256"]
        )
        allocated = await self.post()
        self.assertEqual(allocated.status_code, 202, allocated.text)
        target = self.store.read_dokploy_target_record(
            context_name=self.intent.testing_context, instance_name="testing"
        )
        identity = self.store.read_dokploy_target_id_record(
            context_name=target.context, instance_name=target.instance
        )
        calls = self.provider.call_args_list.copy()
        records_payload.update(
            mode="apply",
            reviewed_plan_sha256=planned.json()["result"]["plan_sha256"],
            resolved_target_id=identity.target_id,
        )
        mismatched = await records(resolved_target_id=self.production_id.target_id)
        self.assertEqual(mismatched.status_code, 400, mismatched.text)
        self.assertEqual(self.store.list_product_profile_records(), (self.profile,))
        applied = await records()
        self.assertEqual(applied.status_code, 202, applied.text)
        registered_target = self.store.read_dokploy_target_record(
            context_name=target.context, instance_name=target.instance
        )
        registered_identity = self.store.read_dokploy_target_id_record(
            context_name=target.context, instance_name=target.instance
        )
        self.assertEqual(registered_identity.target_id, identity.target_id)
        self.assertEqual(registered_target.target_name, target.target_name)
        self.assertEqual(registered_target.project_name, target.project_name)
        replayed = await records()
        self.assertEqual(replayed.status_code, 202, replayed.text)
        self.assertTrue(replayed.json()["replayed"])
        allocation_replay = await self.post()
        self.assertEqual(allocation_replay.status_code, 202, allocation_replay.text)
        self.assertEqual(self.provider.call_args_list, calls)
        self.assert_production_unchanged()
