"""New storage can be prepared while the original lane remains held and unknown."""

import asyncio
from contextlib import nullcontext
import unittest
from unittest.mock import patch
from httpx2 import Response

from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.http_app import launchplane_identity_actor
from control_plane.odoo_testing_resources import GenericWebTestingResources
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    run_durable_provider_operation,
)
from control_plane.service_auth import LocalOperatorIdentity
from control_plane.testing_separation_resources import (
    ROUTE,
    SeparationResourcesAdapter,
    SeparationResourcesRequest,
    SeparationResourcesRefused,
    preparation_binding,
)
from tests import test_testing_event_deploy_recovery as historical
from tests import test_odoo_testing_resources as storage_program


class GenericStorageProgramTests(storage_program.TestingResourceProgramTests):
    def setUp(self) -> None:
        super().setUp()
        self.resources = GenericWebTestingResources(**self.resources.model_dump())

    def test_production_resources_and_ports_are_unchanged(self) -> None:
        production = self.root / "production-db" / "_data"
        production.mkdir(parents=True)
        data = production / "business-data"
        data.write_text("needed production data")
        self.containers["production"] = {
            "Config": {"Labels": {"com.docker.compose.project": "production"}},
            "HostConfig": {"PortBindings": {"3306/tcp": [{"HostPort": "3307"}]}},
            "Mounts": [{"Type": "volume", "Name": "production-db", "Source": str(production)}],
        }
        before = self.containers.copy()
        self.prepare()
        self.assertEqual(data.read_text(), "needed production data")
        self.assertEqual(self.containers, before)
        self.assertEqual(set(self.volumes), set(self.resources.volumes.values()))


class SeparationResourceTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.fixture = historical.TestingEventDeployRecoveryTests()
        self.fixture.database_url = self.database_url
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.root = self.fixture.store, self.fixture.root
        config = patch(
            "control_plane.dokploy.source.read_dokploy_config",
            return_value=("https://provider.test", "fixture-token"),
        )
        config.start()
        self.addCleanup(config.stop)
        if self.store.database_dialect_name != "postgresql":
            # Unit/rehearsal adapter tests; the HTTP route refuses SQLite apply.
            # The inherited PostgreSQL inventory runs the real guard below.
            guard = patch.object(
                self.store, "serialize_disposable_testing_resources", side_effect=nullcontext
            )
            guard.start()
            self.addCleanup(guard.stop)
        self.product, self.context = self.fixture.profile.product, self.fixture.context
        self.actor = launchplane_identity_actor(
            LocalOperatorIdentity(subject="local-owner-agent", token_label="local-owner-write")
        )
        self.original = self.fixture.reserve(phase="deploy_trigger")
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        # Omitted purpose reproduces the retained legacy record. Its prose is
        # deliberately a real staff hold: no reason-string inference is allowed.
        target.policies.staff_testing_hold = DokployTargetStaffTestingHold(
            reason="Staff are using the old lane; never change it.", recorded_by=self.actor
        )
        self.store.write_dokploy_target_record(target)
        self.retained = target
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "instance": "prod",
                    "policies": target.policies.model_copy(update={"staff_testing_hold": None}),
                    "env": {"PRODUCTION_PORT": "3307", "PRODUCTION_VOLUME": "business-db"},
                }
            )
        )
        self.production = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="prod"
        )
        binding, _ = preparation_binding(
            self.store, self.product, self.context, self.actor, self.root
        )
        self.body = SeparationResourcesRequest(
            context=self.context,
            mode="apply",
            reason="Prepare new independent storage without disturbing staff.",
            reviewed_plan_sha256=binding,
        )
        self.adapter = SeparationResourcesAdapter(
            self.root, self.store, self.product, self.body, self.actor, "launchplane_req_fixture"
        )
        self.effects: list[str] = []

    def run_preparation(self, key: str = "prepare") -> DurableProviderOperationResult:
        return run_durable_provider_operation(
            store=self.store,
            scope="fixture-director-agent",
            route_path=ROUTE.replace("{product}", self.product),
            idempotency_key=key,
            request_fingerprint="a" * 64,
            lease_owner="fixture-worker",
            response_trace_id="launchplane_req_fixture",
            adapter=self.adapter,
        )

    def prepare(self, **kwargs: object) -> dict[str, str]:
        callback = kwargs["before_effect"]
        assert callable(callback)
        callback("prepare_new_storage")
        self.effects.append("new empty storage")
        resources = kwargs["resources"]
        assert isinstance(resources, GenericWebTestingResources)
        return {
            "testing_resources_ok": "true",
            "testing_resources_binding_sha256": resources.binding_sha256,
        }

    def assert_retained(self) -> None:
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ),
            self.retained,
        )
        self.assertEqual(
            self.store.read_dokploy_target_record(context_name=self.context, instance_name="prod"),
            self.production,
        )
        self.assertIn(self.original, self.store.list_held_provider_target_reservations())
        self.assertEqual(self.store.list_product_reconcile_requests(), ())

    def test_legacy_staff_hold_and_unknown_deploy_survive_preparation_and_replay(self) -> None:
        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule",
            side_effect=self.prepare,
        ):
            first = self.run_preparation()
            replay = self.run_preparation()
        self.assertEqual(first.response_status_code, 202)
        self.assertEqual(replay.response_status_code, 202)
        self.assertEqual(self.effects, ["new empty storage"])
        self.assert_retained()

    def test_lost_reply_retains_both_fences_and_never_redispatches(self) -> None:
        def lost(**kwargs: object) -> dict[str, str]:
            if kwargs.get("mode") == "readback":
                raise OSError("Remote readback is unavailable")
            self.prepare(**kwargs)
            raise OSError("Lost remote schedule outcome")

        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule", side_effect=lost
        ):
            for key in ("prepare", "prepare", "new-key"):
                self.assertEqual(self.run_preparation(key).response_status_code, 409)
        self.assertEqual(self.effects, ["new empty storage"])
        self.assertEqual(len(self.store.list_held_provider_target_reservations()), 2)
        self.assert_retained()

    def test_original_key_adopts_only_actual_complete_storage_readback(self) -> None:
        def lost_then_read(**kwargs: object) -> dict[str, str]:
            if kwargs.get("mode") == "readback":
                resources = kwargs["resources"]
                assert isinstance(resources, GenericWebTestingResources)
                return {
                    "testing_resources_ok": "true",
                    "testing_resources_binding_sha256": resources.binding_sha256,
                }
            self.prepare(**kwargs)
            raise OSError("Lost completed schedule response")

        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule",
            side_effect=lost_then_read,
        ):
            self.assertEqual(self.run_preparation().response_status_code, 409)
            recovered = self.run_preparation()
            self.assertEqual(recovered.response_status_code, 202)
            replay = self.run_preparation()
            self.assertEqual(replay.response_status_code, 202)
        self.assertEqual(self.effects, ["new empty storage"])
        self.assertEqual(self.store.list_held_provider_target_reservations(), (self.original,))
        self.assert_retained()

    def test_foreign_hold_and_production_alias_refuse_before_effect(self) -> None:
        from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord

        hold = self.retained.policies.staff_testing_hold
        assert hold is not None
        changed = self.retained.model_copy(deep=True)
        changed.policies.staff_testing_hold = hold.model_copy(
            update={"recorded_by": "another-agent"}
        )
        self.store.write_dokploy_target_record(changed)
        with self.assertRaises(ValueError):
            self.adapter.plan()
        self.store.write_dokploy_target_record(self.retained)
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=self.context,
                instance="prod",
                target_id=self.fixture.target.resolved_target.target_id,
                updated_at=self.fixture.now(),
            )
        )
        with self.assertRaises(ValueError):
            self.adapter.plan()
        self.assertEqual(self.effects, [])

    def test_changed_authority_after_dispatch_is_unknown_and_preserves_original(self) -> None:
        def drift(**kwargs: object) -> dict[str, str]:
            result = self.prepare(**kwargs)
            changed = self.retained.model_copy(deep=True)
            changed.policies.staff_testing_hold = None
            self.store.write_dokploy_target_record(changed)
            return result

        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule", side_effect=drift
        ):
            for _ in range(2):
                self.assertEqual(self.run_preparation().response_status_code, 409)
        self.assertEqual(self.effects, ["new empty storage"])
        self.assertIn(self.original, self.store.list_held_provider_target_reservations())

    def test_unknown_provider_inventory_refuses_without_any_schedule(self) -> None:
        self.fixture.responses["/api/deployment.queueList"] = {"items": [], "hasMore": True}
        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule"
        ) as schedule:
            with self.assertRaises(SeparationResourcesRefused):
                self.run_preparation()
            schedule.assert_not_called()
        self.assert_retained()

    def test_retired_product_cannot_allocate_resources(self) -> None:
        profile = self.store.read_product_profile_record(self.product)
        retired = profile.model_copy(deep=True)
        retired.lifecycle_state = "retired"
        retired.preview.enabled = False
        self.store.write_product_profile_record(retired)
        with self.assertRaises(SeparationResourcesRefused):
            self.adapter.plan()

    def test_empty_storage_carries_no_production_credential(self) -> None:
        from control_plane.contracts.secret_record import SecretRecord, SecretBinding
        from control_plane.contracts.runtime_key_safety_policy import (
            RuntimeKeySafetyPolicyRecord,
            RuntimeSecretSafetyRule,
        )
        from control_plane.live_target_runtime import (
            evaluate_runtime_key_safety_for_live_target_sync,
            LiveTargetRuntimeError,
        )

        timestamp = self.fixture.now()
        secret = SecretRecord(
            secret_id="production-only",
            scope="context_instance",
            integration="runtime_environment",
            name="SYNC_DB_PASSWORD",
            context=self.context,
            instance="prod",
            current_version_id="production-version",
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.store.write_secret_record(secret)
        self.store.write_secret_binding(
            SecretBinding(
                binding_id="shared-binding",
                secret_id=secret.secret_id,
                integration="runtime_environment",
                binding_key="SYNC_DB_PASSWORD",
                context=self.context,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        self.store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="production-only-policy",
                source="fixture",
                updated_at=timestamp,
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key="SYNC_DB_PASSWORD", secret_class="prod_only"
                    ),
                ),
            )
        )
        self.body.reviewed_plan_sha256 = preparation_binding(
            self.store, self.product, self.context, self.actor, self.root
        )[0]
        with self.assertRaises(LiveTargetRuntimeError):
            evaluate_runtime_key_safety_for_live_target_sync(
                record_store=self.store, context_name=self.context, instance_name="testing"
            )
        before = self.store.list_secret_bindings()
        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule",
            side_effect=self.prepare,
        ):
            result = self.run_preparation()
        self.assertEqual(result.response_status_code, 202)
        payload = result.response_payload["result"]
        assert isinstance(payload, dict)
        self.assertFalse(payload["credentials_installed"])
        self.assertEqual(self.store.list_secret_bindings(), before)
        self.assertEqual(self.store.read_secret_record(secret.secret_id), secret)
        self.assert_retained()

    def test_existing_preparation_schedule_is_never_overwritten_or_redispatched(self) -> None:
        from control_plane.testing_separation_resources import run_preparation_schedule
        from unittest.mock import Mock

        resources = GenericWebTestingResources(
            product=self.product,
            context=self.context,
            operation_id="fixture",
            target_id=self.fixture.target.resolved_target.target_id,
            namespace="lp-testing-" + "a" * 32,
        )
        checkpoint = Mock()
        with (
            patch(
                "control_plane.testing_separation_resources._find_storage_schedule",
                return_value={"scheduleId": "retained-preparation"},
            ),
            patch("control_plane.dokploy.post_deploy.run_manual_data_schedule") as schedule,
        ):
            with self.assertRaises(ValueError):
                run_preparation_schedule(
                    root=self.root, resources=resources, before_effect=checkpoint
                )
        checkpoint.assert_not_called()
        schedule.assert_not_called()
        self.assert_retained()

    def test_readback_schedule_requires_terminal_original_provenance_and_never_repeats_creation(
        self,
    ) -> None:
        from control_plane.testing_separation_resources import run_preparation_schedule
        from unittest.mock import Mock

        resources = self.adapter.resources(self.body.reviewed_plan_sha256)
        schedules = []

        def run(**kwargs: object) -> dict[str, str]:
            payload = kwargs["schedule_payload"]
            assert isinstance(payload, dict)
            schedules.append(payload.copy())
            self.fixture.responses["/api/schedule.list"] = [
                {**payload, "scheduleId": "fixture-preparation"}
            ]
            return {
                "testing_resources_ok": "true",
                "testing_resources_binding_sha256": resources.binding_sha256,
            }

        with patch("control_plane.dokploy.post_deploy.run_manual_data_schedule", side_effect=run):
            run_preparation_schedule(root=self.root, resources=resources, before_effect=Mock())
        history = {
            "scheduleId": "fixture-preparation",
            "deploymentId": "fixture-creation",
            "status": "done",
            "startedAt": self.fixture.now(-10),
            "finishedAt": self.fixture.now(-5),
        }
        for status in ("done", "error"):
            self.fixture.responses["/api/deployment.allByType"] = [{**history, "status": status}]
            with patch(
                "control_plane.dokploy.post_deploy.run_manual_data_schedule",
                return_value={
                    "schedule_id": "fixture-readback",
                    "testing_resources_ok": "true",
                    "testing_resources_binding_sha256": resources.binding_sha256,
                },
            ) as reader:
                result = run_preparation_schedule(
                    root=self.root, resources=resources, mode="readback", before_effect=Mock()
                )
                self.assertEqual(
                    reader.call_args.kwargs["schedule_payload"]["script"],
                    resources.script("readback"),
                )
                self.assertEqual(
                    result["testing_resources_binding_sha256"], resources.binding_sha256
                )
                receipt = self.adapter.result(resources, result)
                self.assertEqual(receipt["schedule_id"], history["scheduleId"])
                self.assertEqual(receipt["readback_schedule_id"], result["schedule_id"])
        self.assertEqual(len(schedules), 1)
        for invalid in (
            [],
            {"items": [], "hasMore": True},
            [{**history, "status": "running"}],
            [history, history],
            [{**history, "scheduleId": "other-schedule"}],
            [{**history, "finishedAt": None}],
        ):
            self.fixture.responses["/api/deployment.allByType"] = invalid
            with (
                self.subTest(history=invalid),
                patch("control_plane.dokploy.post_deploy.run_manual_data_schedule") as reader,
            ):
                with self.assertRaises(ValueError):
                    run_preparation_schedule(
                        root=self.root, resources=resources, mode="readback", before_effect=Mock()
                    )
                reader.assert_not_called()
        self.fixture.responses["/api/deployment.allByType"] = [history]
        original = self.fixture.responses["/api/schedule.list"]
        assert isinstance(original, list) and isinstance(original[0], dict)
        for change in ({"script": resources.script("readback")}, {"enabled": True}):
            self.fixture.responses["/api/schedule.list"] = [{**original[0], **change}]
            with patch("control_plane.dokploy.post_deploy.run_manual_data_schedule") as reader:
                with self.assertRaises(ValueError):
                    run_preparation_schedule(
                        root=self.root, resources=resources, mode="readback", before_effect=Mock()
                    )
                reader.assert_not_called()
        self.fixture.responses["/api/schedule.list"] = [
            original[0],
            {
                **original[0],
                "name": str(original[0]["name"]) + " readback",
                "script": resources.script("readback"),
                "scheduleId": "active-readback",
            },
        ]

        def read(**kwargs: object) -> object:
            if kwargs["path"] == "/api/deployment.allByType":
                query = kwargs["query"]
                assert isinstance(query, dict)
                return (
                    [history]
                    if query["id"] == history["scheduleId"]
                    else [{**history, "scheduleId": query["id"], "status": "running"}]
                )
            return self.fixture.read(**kwargs)

        with (
            patch("control_plane.dokploy.api.dokploy_request", side_effect=read),
            patch("control_plane.dokploy.post_deploy.run_manual_data_schedule") as reader,
        ):
            with self.assertRaises(ValueError):
                run_preparation_schedule(
                    root=self.root, resources=resources, mode="readback", before_effect=Mock()
                )
            reader.assert_not_called()
        self.assert_retained()

    def test_absent_effect_checkpoint_is_not_provider_absence_proof(self) -> None:
        with patch("control_plane.dokploy.post_deploy.run_manual_data_schedule") as dispatch:
            observed = self.adapter.observe(
                "original-operation", "", self.body.reviewed_plan_sha256
            )
        self.assertEqual(observed.outcome, "unknown")
        dispatch.assert_not_called()
        self.assert_retained()

    def test_provider_placement_drift_refuses_before_any_new_storage(self) -> None:
        self.fixture.payload["serverId"] = "different-server"
        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule"
        ) as schedule:
            with self.assertRaises(SeparationResourcesRefused):
                self.run_preparation()
            schedule.assert_not_called()
        self.assert_retained()

    def test_http_plan_and_apply_use_the_same_identity_and_preserve_replay(self) -> None:
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_auth import BearerIdentityConfig
        from tests.support.auth import (
            StubVerifier,
            identity,
            local_operator_policy,
            scoped_agent_transport,
        )
        from tests.support.http import request

        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=local_operator_policy(actions=()),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="fixture",
                local_operator_subject="local-owner-agent",
                local_operator_token_label="local-owner-write",
            ),
        )

        async def call(**changes: object) -> Response:
            return await request(
                app,
                "POST",
                ROUTE.replace("{product}", self.product),
                headers={"Authorization": "Bearer fixture", "Idempotency-Key": "http-prepare"},
                payload={**self.body.model_dump(mode="json"), **changes},
            )

        plan = asyncio.run(call(mode="dry-run", reviewed_plan_sha256=""))
        self.assertEqual(plan.status_code, 202, plan.text)
        with scoped_agent_transport():
            denied = asyncio.run(call(mode="dry-run"))
        self.assertEqual(denied.status_code, 403, denied.text)
        for injected in (
            {"instance": "prod"},
            {"target_id": "production"},
            {"volumes": {"database": "production-db"}},
            {"credentials": {"password": "production-key"}},
            {"deploy": True},
        ):
            with self.subTest(injected=tuple(injected)):
                rejected = asyncio.run(call(mode="dry-run", **injected))
                self.assertEqual(rejected.status_code, 400, rejected.text)
        with patch(
            "control_plane.testing_separation_resources.run_preparation_schedule",
            side_effect=self.prepare,
        ):
            result = asyncio.run(call())
            if self.store.database_dialect_name == "postgresql":
                self.assertEqual(result.status_code, 202, result.text)
                replay = asyncio.run(call())
                self.assertEqual(replay.status_code, 202, replay.text)
                self.assertEqual(self.effects, ["new empty storage"])
                stored = result.json()["result"]
                changed = self.retained.model_copy(deep=True)
                changed.env["NEW_CONFIG"] = "different"
                self.store.write_dokploy_target_record(changed)
                receipt = asyncio.run(
                    call(mode="receipt", reason="Read stored original preparation.")
                )
                self.assertEqual(receipt.status_code, 202, receipt.text)
                self.assertEqual(receipt.json()["result"], stored)
                self.store.write_dokploy_target_record(self.retained)
            else:
                self.assertEqual(result.status_code, 503, result.text)
                self.assertFalse(self.effects)
        self.assert_retained()
