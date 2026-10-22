"""Configuration staging protects production without asserting disposable lane facts."""

from httpx2 import Response
from unittest.mock import patch

from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.http_app import launchplane_identity_actor
from control_plane.live_target_runtime import LiveTargetRuntimeApplyEnvelope
from control_plane.service_auth import LocalOperatorIdentity
from control_plane.testing_runtime_separation import separation_binding
from tests import test_disposable_lane_authority as authority
from tests.support.auth import scoped_agent_transport
from tests.support.http import request


class TestingRuntimeSeparationTests(authority._DisposableRuntimeAuthorityFixture):
    database_url: str | None = None

    def setUp(self) -> None:
        super().setUp()
        from control_plane.contracts.secret_record import SecretRecord, SecretBinding

        self.production_secret = SecretRecord(
            secret_id="production-only",
            scope="context_instance",
            integration="runtime_environment",
            name="SYNC_DB_PASSWORD",
            context=self.context,
            instance="prod",
            current_version_id="production-version",
            created_at="2026-10-15T00:00:00Z",
            updated_at="2026-10-15T00:00:00Z",
        )
        self.store.write_secret_record(self.production_secret)
        self.store.write_secret_binding(
            SecretBinding(
                binding_id="production-binding",
                secret_id=self.production_secret.secret_id,
                integration="runtime_environment",
                binding_key="SYNC_DB_PASSWORD",
                context=self.context,
                instance="prod",
                declared_secret_class="prod_only",
                created_at=self.production_secret.created_at,
                updated_at=self.production_secret.updated_at,
            )
        )
        self.actor = launchplane_identity_actor(
            LocalOperatorIdentity(subject="local-owner-agent", token_label="local-owner-write")
        )
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        target.policies.staff_testing_hold = DokployTargetStaffTestingHold(
            purpose="isolation_staging",
            reason="Stage independent testing resources.",
            recorded_by=self.actor,
        )
        self.store.write_dokploy_target_record(target)
        self.payload = {
            "product": "example-site",
            "context": self.context,
            "instance": "testing",
            "mode": "apply",
            "separation_only": True,
        }
        self.binding, self.key = separation_binding(
            self.store,
            LiveTargetRuntimeApplyEnvelope.model_validate({**self.payload, "mode": "dry-run"}),
            self.actor,
        )
        self.payload["reviewed_binding_sha256"] = self.binding

    async def stage(self, *, key: str = "separation", **changes: object) -> Response:
        return await request(
            self.app,
            "POST",
            "/v1/live-target-runtime/apply",
            headers={"Authorization": "Bearer local-operator-token", "Idempotency-Key": key},
            payload={**self.payload, **changes},
        )

    async def test_unknown_usage_custody_and_sharing_do_not_block_env_only_staging(self) -> None:
        from tests.support.disposable_lanes import record_safety

        for index, facts in enumerate(
            (
                None,
                {"usage": "unknown", "data": "unknown", "production_resources": "shared"},
                {"usage": "unknown", "data": "only_copy", "production_resources": "shared"},
            )
        ):
            if facts is not None:
                record_safety(self.store, "example-site", self.context, "testing", **facts)
            with (
                self.subTest(facts=facts),
                patch(
                    "control_plane.live_target_runtime.evaluate_runtime_key_safety_for_live_target_sync"
                ),
                patch(
                    "control_plane.live_target_runtime.apply_live_target_runtime_environment",
                    return_value={"status": "ok"},
                ) as driver,
            ):
                response = await self.stage(key=f"separation-{index}")
                self.assertEqual(response.status_code, 202, response.text)
                from control_plane.http_routes.mutation_support import idempotency_scope

                reservation = self.store.read_idempotency_record(
                    scope=idempotency_scope(
                        LocalOperatorIdentity(
                            subject="local-owner-agent", token_label="local-owner-write"
                        )
                    ),
                    route_path="/v1/live-target-runtime/apply",
                    idempotency_key=f"separation-{index}",
                )
                assert reservation is not None
                capsule = self.store.read_provider_target_origin(reservation)
                assert capsule is not None
                target = self.store.read_provider_target_record(
                    context_name=self.context, instance_name="testing"
                )
                self.assertEqual(
                    (
                        capsule.origin.product,
                        capsule.origin.context,
                        capsule.origin.instance,
                        capsule.origin.target_id,
                    ),
                    (self.payload["product"], self.context, "testing", target.target_id),
                )
                self.assertEqual(self.store.read_original_mutation_intent(reservation), {})
                self.assertFalse(driver.call_args.kwargs["deploy"])
                self.assertEqual(driver.call_args.kwargs["instance_name"], "testing")

    async def test_staff_foreign_and_legacy_holds_refuse_before_any_driver_call(self) -> None:
        for purpose, actor in (("staff_testing", self.actor), ("isolation_staging", "other-agent")):
            target = self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            )
            target.policies.staff_testing_hold = DokployTargetStaffTestingHold.model_validate(
                {"purpose": purpose, "reason": "Do not interrupt.", "recorded_by": actor}
            )
            self.store.write_dokploy_target_record(target)
            with (
                self.subTest(purpose=purpose, actor=actor),
                patch(
                    "control_plane.live_target_runtime.apply_live_target_runtime_environment"
                ) as driver,
            ):
                response = await self.stage()
                self.assertEqual(response.status_code, 409, response.text)
                driver.assert_not_called()

    async def test_production_and_deploy_flags_cannot_enter_separation(self) -> None:
        with patch(
            "control_plane.live_target_runtime.apply_live_target_runtime_environment"
        ) as driver:
            for changes in (
                {"instance": "prod"},
                {"deploy": True},
                {"no_cache": True},
                {"instance": "pr-1"},
            ):
                response = await self.stage(**changes)
                self.assertEqual(response.status_code, 400, response.text)
            with scoped_agent_transport():
                response = await self.stage()
            self.assertEqual(response.status_code, 403, response.text)
            driver.assert_not_called()

    async def test_unknown_deploy_fence_is_preserved_and_no_write_runs(self) -> None:
        from tests.support.provider_target_history import reserve_historical_mutation

        reserved = reserve_historical_mutation(
            self.store,
            scope="earlier",
            route_path="/earlier-deploy",
            idempotency_key="old-deploy",
            request_fingerprint="a" * 64,
            lease_owner="old-worker",
            reconciliation_key="old-evidence",
            provider_target_key=self.key,
        )
        assert reserved.record is not None
        fenced = self.store.mark_mutation_reconcile_required(
            reservation=reserved.record, reconciliation_key="old-evidence"
        )
        with patch(
            "control_plane.live_target_runtime.apply_live_target_runtime_environment"
        ) as driver:
            response = await self.stage()
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(response.json()["error"]["code"], "mutation_reconciliation_required")
            driver.assert_not_called()
        self.assertIn(fenced.record, self.store.list_held_provider_target_reservations())

    async def test_new_production_alias_refuses_even_without_observations(self) -> None:
        target = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target.model_copy(update={"context": "other-product", "instance": "prod"})
        )
        with patch(
            "control_plane.live_target_runtime.apply_live_target_runtime_environment"
        ) as driver:
            response = await self.stage()
            self.assertEqual(response.status_code, 409, response.text)
            driver.assert_not_called()

    async def test_late_hold_refuses_before_effect_and_releases_only_fresh_reservation(
        self,
    ) -> None:
        def mutate_then_checkpoint(**kwargs: object) -> dict[str, object]:
            target = self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            )
            target.policies.staff_testing_hold = DokployTargetStaffTestingHold(
                reason="Staff testing now."
            )
            self.store.write_dokploy_target_record(target)
            callback = kwargs["before_provider_mutation"]
            assert callable(callback)
            callback()
            self.fail("No provider effect may follow a new staff hold.")

        with (
            patch(
                "control_plane.live_target_runtime.evaluate_runtime_key_safety_for_live_target_sync"
            ),
            patch(
                "control_plane.live_target_runtime.apply_live_target_runtime_environment",
                side_effect=mutate_then_checkpoint,
            ),
        ):
            response = await self.stage()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

    async def test_timeout_after_write_keeps_unknown_fence_across_keys(self) -> None:
        effects = []

        def write_and_lose_reply(**kwargs: object) -> dict[str, object]:
            if not kwargs["apply_changes"]:
                raise OSError("read still unavailable")
            callback = kwargs["before_provider_mutation"]
            assert callable(callback)
            callback()
            effects.append("env-only")
            raise OSError("lost reply")

        with (
            patch(
                "control_plane.live_target_runtime.evaluate_runtime_key_safety_for_live_target_sync"
            ),
            patch(
                "control_plane.live_target_runtime.apply_live_target_runtime_environment",
                side_effect=write_and_lose_reply,
            ),
        ):
            for key in ("separation", "separation", "another-key"):
                response = await self.stage(key=key)
                self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(effects, ["env-only"])
        self.assertEqual(len(self.store.list_held_provider_target_reservations()), 1)

    async def test_real_env_writer_stages_new_volume_and_port_settings_and_keeps_production(
        self,
    ) -> None:
        from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
        from control_plane.dokploy import source
        from control_plane.dokploy.api import parse_dokploy_env_text
        from control_plane.runtime_environments import SiteRuntimeEnvironment

        desired = {
            "SYNC_DB_VOLUME_NAME": "new-testing-db",
            "SYNC_CONFIG_VOLUME_NAME": "new-testing-config",
            "SYNC_DB_HOST_PORT": "13307",
            "SYNC_HEALTH_HOST_PORT": "18000",
            "SYNC_DB_PASSWORD": "fixture-testing-credential",
        }
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context=self.context,
                instance="testing",
                env={k: v for k, v in desired.items() if k != "SYNC_DB_PASSWORD"},
                updated_at="2026-10-15T00:00:00Z",
            )
        )
        binding, _ = separation_binding(
            self.store,
            LiveTargetRuntimeApplyEnvelope.model_validate({**self.payload, "mode": "dry-run"}),
            self.actor,
        )
        self.payload["reviewed_binding_sha256"] = binding
        production = {
            "composeId": "target-prod",
            "env": "SYNC_DB_VOLUME_NAME=production-db\nSYNC_CONFIG_VOLUME_NAME=production-config\nSYNC_DB_HOST_PORT=3307\nSYNC_HEALTH_HOST_PORT=8000\nSYNC_DB_PASSWORD=fixture-production-credential\n",
        }
        testing = {"composeId": "target-testing", "env": production["env"]}
        records_before = (
            self.store.read_dokploy_target_record(context_name=self.context, instance_name="prod"),
            self.store.list_secret_bindings(),
            self.store.list_secret_records(),
        )
        calls = []

        def fetch(**kwargs: object) -> dict[str, str]:
            self.assertEqual(kwargs["target_id"], "target-testing")
            return testing.copy()

        def update(**kwargs: object) -> None:
            payload = kwargs["payload"]
            assert isinstance(payload, dict)
            self.assertEqual(set(payload), {"composeId", "env"})
            self.assertEqual(payload["composeId"], "target-testing")
            calls.append(payload)
            testing["env"] = payload["env"]

        with (
            patch(
                "control_plane.live_target_runtime.dokploy_source.read_control_plane_dokploy_source_of_truth",
                return_value=source.DokploySourceOfTruth(
                    schema_version=1,
                    targets=(
                        source.DokployTargetDefinition(
                            context=self.context, instance="testing", target_id="target-testing"
                        ),
                    ),
                ),
            ),
            patch(
                "control_plane.live_target_runtime.control_plane_runtime_environments.resolve_site_runtime_environment",
                return_value=SiteRuntimeEnvironment(
                    values=desired,
                    secret_keys=frozenset({"SYNC_DB_PASSWORD"}),
                    retired_keys=frozenset(),
                    site_keys=frozenset(desired),
                ),
            ),
            patch(
                "control_plane.live_target_runtime.dokploy_source.read_dokploy_config",
                return_value=("https://provider.invalid", "fixture-token"),
            ),
            patch(
                "control_plane.live_target_runtime.dokploy_api.fetch_dokploy_target_payload",
                side_effect=fetch,
            ),
            patch("control_plane.dokploy.api.dokploy_request", side_effect=update),
            patch(
                "control_plane.live_target_runtime.trigger_and_wait_for_dokploy_target_deploy"
            ) as deploy,
        ):
            response = await self.stage()
            self.assertEqual(response.status_code, 202, response.text)
            replay = await self.stage()
            self.assertEqual(replay.status_code, 202, replay.text)
            deploy.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertEqual(parse_dokploy_env_text(testing["env"]), desired)
        self.assertEqual(
            records_before,
            (
                self.store.read_dokploy_target_record(
                    context_name=self.context, instance_name="prod"
                ),
                self.store.list_secret_bindings(),
                self.store.list_secret_records(),
            ),
        )
        self.assertNotIn("fixture-testing-credential", response.text)
        self.assertNotIn("fixture-production-credential", response.text)

    async def test_key_safety_refusal_cannot_reach_writer(self) -> None:
        from control_plane.contracts.secret_record import SecretBinding
        from control_plane.contracts.runtime_key_safety_policy import (
            RuntimeKeySafetyPolicyRecord,
            RuntimeSecretSafetyRule,
        )

        self.store.write_secret_binding(
            SecretBinding(
                binding_id="shared-production-binding",
                secret_id=self.production_secret.secret_id,
                integration="runtime_environment",
                binding_key="SYNC_DB_PASSWORD",
                context=self.context,
                created_at=self.production_secret.created_at,
                updated_at=self.production_secret.updated_at,
            )
        )
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="production-policy",
                source="fixture",
                updated_at=self.production_secret.updated_at,
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="SYNC_DB_PASSWORD", secret_class="prod_only"
                    ),
                ),
            )
        )
        binding, _ = separation_binding(
            self.store,
            LiveTargetRuntimeApplyEnvelope.model_validate({**self.payload, "mode": "dry-run"}),
            self.actor,
        )
        self.payload["reviewed_binding_sha256"] = binding
        with patch(
            "control_plane.live_target_runtime.apply_live_target_runtime_environment"
        ) as driver:
            response = await self.stage()
            self.assertEqual(response.status_code, 409, response.text)
            driver.assert_not_called()
        self.assertEqual(
            self.store.read_secret_record(self.production_secret.secret_id), self.production_secret
        )

    async def test_http_dry_run_returns_a_bound_plan_without_a_provider_write(self) -> None:
        with patch(
            "control_plane.live_target_runtime.apply_live_target_runtime_environment",
            return_value={"status": "ok"},
        ) as driver:
            response = await self.stage(mode="dry-run", reviewed_binding_sha256="")
            self.assertEqual(response.status_code, 202, response.text)
            self.assertEqual(response.json()["result"]["separation_binding_sha256"], self.binding)
            self.assertFalse(driver.call_args.kwargs["apply_changes"])
            self.assertFalse(driver.call_args.kwargs["deploy"])

    async def test_original_key_adopts_a_proven_env_write_without_redispatch(self) -> None:
        effects = []

        def write_then_observe(**kwargs: object) -> dict[str, object]:
            if kwargs["apply_changes"]:
                callback = kwargs["before_provider_mutation"]
                assert callable(callback)
                callback()
                effects.append("env-only")
                raise OSError("lost reply")
            return {
                "status": "ok",
                "runtime_environment": {"changed_keys": []},
                "deploy": {"requested": False, "triggered": False},
            }

        with (
            patch(
                "control_plane.live_target_runtime.evaluate_runtime_key_safety_for_live_target_sync"
            ),
            patch(
                "control_plane.live_target_runtime.apply_live_target_runtime_environment",
                side_effect=write_then_observe,
            ),
        ):
            lost = await self.stage()
            self.assertEqual(lost.status_code, 409, lost.text)
            recovered = await self.stage()
            self.assertEqual(recovered.status_code, 202, recovered.text)
            self.assertTrue(recovered.json()["result"]["apply"]["recovered_by_readback"])
            replay = await self.stage()
            self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(effects, ["env-only"])
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

    async def test_uncertain_original_key_stays_reconciliation_required_after_input_drift(
        self,
    ) -> None:
        from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord

        effects = []
        reads = []

        def write_and_drift(**kwargs: object) -> dict[str, object]:
            if not kwargs["apply_changes"]:
                reads.append("readback")
                return {
                    "runtime_environment": {"changed_keys": []},
                    "deploy": {"requested": False, "triggered": False},
                }
            callback = kwargs["before_provider_mutation"]
            assert callable(callback)
            callback()
            effects.append("env-only")
            self.store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context=self.context,
                    instance="testing",
                    env={"EXAMPLE_CONFIG": "new"},
                    updated_at=self.production_secret.updated_at,
                )
            )
            raise OSError("lost write reply")

        with (
            patch(
                "control_plane.live_target_runtime.evaluate_runtime_key_safety_for_live_target_sync"
            ),
            patch(
                "control_plane.live_target_runtime.apply_live_target_runtime_environment",
                side_effect=write_and_drift,
            ),
        ):
            for _ in range(2):
                response = await self.stage()
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    response.json()["error"]["code"], "mutation_reconciliation_required"
                )
        self.assertEqual(effects, ["env-only"])
        self.assertEqual(reads, [])
        self.assertEqual(len(self.store.list_held_provider_target_reservations()), 1)

    async def test_last_key_safety_checkpoint_refuses_before_write(self) -> None:
        from control_plane.live_target_runtime import LiveTargetRuntimeError

        effects = []

        def write(**kwargs: object) -> dict[str, object]:
            callback = kwargs["before_provider_mutation"]
            assert callable(callback)
            callback()
            effects.append("write")
            return {}

        with (
            patch(
                "control_plane.live_target_runtime.evaluate_runtime_key_safety_for_live_target_sync",
                side_effect=[{}, LiveTargetRuntimeError("Credential became unsafe.")],
            ),
            patch(
                "control_plane.live_target_runtime.apply_live_target_runtime_environment",
                side_effect=write,
            ),
        ):
            response = await self.stage()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(effects, [])
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())

    async def test_post_write_database_failure_is_not_misreported_as_a_safe_refusal(self) -> None:
        from sqlalchemy.exc import SQLAlchemyError

        effects = []

        def write(**kwargs: object) -> dict[str, object]:
            callback = kwargs["before_provider_mutation"]
            assert callable(callback)
            callback()
            effects.append("env-only")
            return {"status": "ok"}

        with (
            patch(
                "control_plane.live_target_runtime.apply_live_target_runtime_environment",
                side_effect=write,
            ),
            patch.object(
                self.store,
                "complete_mutation_reservation",
                side_effect=SQLAlchemyError("database completion lost"),
            ),
        ):
            with self.assertRaises(SQLAlchemyError):
                await self.stage()
        self.assertEqual(effects, ["env-only"])
        self.assertTrue(self.store.list_held_provider_target_reservations())
        retry = await self.stage()
        self.assertEqual(retry.status_code, 409, retry.text)
        self.assertIn(
            retry.json()["error"]["code"],
            {"mutation_in_progress", "mutation_reconciliation_required"},
        )
