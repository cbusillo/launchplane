from datetime import datetime, timedelta, timezone
from typing import Any
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.disposable_lanes import record_safety
from tests.support.http import request
from tests.support.profiles import _generic_site_profile_payload


class DisposableRuntimeAuthorityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'state.db'}")
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        profile = LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
        self.store.write_product_profile_record(
            profile.model_copy(update={"production_use": "live"})
        )
        for lane in profile.lanes:
            self.store.write_provider_target_record(
                ProviderTargetRecord(
                    context=lane.context,
                    instance=lane.instance,
                    display_name=f"target-{lane.instance}",
                    provider_id="dokploy",
                    target_category="application",
                    provider_target_type="compose",
                    target_id=f"target-{lane.instance}",
                    updated_at=profile.updated_at,
                )
            )
            self.store.write_dokploy_target_record(
                DokployTargetRecord(
                    context=lane.context, instance=lane.instance, updated_at=profile.updated_at
                )
            )
            self.store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context=lane.context,
                    instance=lane.instance,
                    target_id=f"target-{lane.instance}",
                    updated_at=profile.updated_at,
                )
            )
        self.context = next(lane.context for lane in profile.lanes if lane.instance == "testing")
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
            control_plane_root_path=root,
        )

    async def sync(self, *, instance: str = "testing", deploy: bool = False) -> int:
        response = await request(
            self.app,
            "POST",
            "/v1/live-target-runtime/apply",
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": f"sync-{instance}-{deploy}",
            },
            payload={
                "mode": "apply",
                "product": "example-site",
                "context": self.context,
                "instance": instance,
                "deploy": deploy,
            },
        )
        return response.status_code

    async def observe(self, **changes: object) -> Any:
        payload = {
            "mode": "inspect",
            "product": "example-site",
            "context": self.context,
            "instance": "testing",
            **changes,
        }
        return await request(
            self.app,
            "POST",
            "/v1/disposable-lanes/observations",
            headers={"Authorization": "Bearer local-operator-token"},
            payload=payload,
        )

    async def test_bounded_observer_records_actor_and_leaves_lane_config_unchanged(self) -> None:
        initial = self.store.read_product_profile_record("example-site")
        inspected = await self.observe()
        self.assertEqual(inspected.status_code, 202, inspected.text)
        proof = inspected.json()["result"]["binding_sha256"]
        recorded = await self.observe(
            mode="record",
            expected_binding_sha256=proof,
            usage="unused",
            data="reproducible",
            production_resources="isolated",
            usage_evidence="fixture:usage",
            data_evidence="fixture:data",
            isolation_evidence="fixture:resources",
        )
        self.assertEqual(recorded.status_code, 202, recorded.text)
        stored = self.store.read_product_profile_record("example-site")
        self.assertEqual(stored.model_copy(update={"disposable_lane_safety": ()}), initial)
        self.assertTrue(stored.disposable_lane_safety[0].observed_by)
        self.assertEqual(stored.disposable_lane_safety[0].observer_kind, "director_agent")
        with scoped_agent_transport():
            denied = await self.observe(mode="record", expected_binding_sha256=proof)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.store.read_product_profile_record("example-site"), stored)
        denied = await self.observe(instance="prod")
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual(self.store.read_product_profile_record("example-site"), stored)

    async def test_generic_profile_writer_cannot_manufacture_safety(self) -> None:
        from control_plane.service_auth import LocalOperatorPolicyRule

        policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            local_operators=(
                LocalOperatorPolicyRule(
                    subjects=("local-owner-agent",),
                    token_labels=("local-owner-write",),
                    products=("example-site",),
                    contexts=("launchplane",),
                    actions=("product_profile.write",),
                ),
            ),
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy,
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
        )
        profile = self.store.read_product_profile_record("example-site")
        record_safety(self.store, "example-site", self.context, "testing")
        forged = self.store.read_product_profile_record("example-site").model_dump(mode="json")
        self.store.write_product_profile_record(profile)
        with scoped_agent_transport():
            response = await request(
                self.app,
                "POST",
                "/v1/product-profiles",
                headers={
                    "Authorization": "Bearer local-operator-token",
                    "Idempotency-Key": "forged-facts",
                },
                payload=forged,
            )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.store.read_product_profile_record("example-site"), profile)

    async def test_rebuild_preparation_captures_the_stored_target_once_under_the_agent_identity(
        self,
    ) -> None:
        from control_plane.contracts.odoo_stable_target_replacement import (
            OdooStableTargetReplacementApplyRequest,
        )
        from control_plane.contracts.odoo_stable_target_replacement_operation import (
            OdooStableTargetReplacementOperationRecord,
        )
        from control_plane.launchplane_reconcile_authorization import (
            build_launchplane_reconcile_authorization,
        )

        stamp = datetime.now(timezone.utc).isoformat()
        operation = OdooStableTargetReplacementOperationRecord(
            schema_version=2,
            operation_id="held-fixture",
            product="example-site",
            context=self.context,
            instance="testing",
            idempotency_key="fixture-held",
            request_fingerprint="fixture",
            created_at=stamp,
            updated_at=stamp,
            status="reconciliation_required",
            phase="apply",
            error_code="recovery_journal_absent",
            error_message="Unknown historical effects.",
            request=OdooStableTargetReplacementApplyRequest(
                product="example-site",
                instance="testing",
                strategy="recreate-in-place",
                data_source_mode="existing",
                allow_empty_data=True,
            ),
            authorization=build_launchplane_reconcile_authorization(
                product="example-site",
                context=self.context,
                authorized_at=stamp,
            ),
        )
        self.store.write_odoo_stable_target_replacement_operation_record(operation)
        target = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        inspected = await self.observe()
        prepared = await self.observe(
            mode="prepare-rebuild",
            replaces_operation_id=operation.operation_id,
            expected_binding_sha256=inspected.json()["result"]["binding_sha256"],
        )
        self.assertEqual(prepared.status_code, 202, prepared.text)
        captured = self.store.read_odoo_stable_target_replacement_operation_record(
            operation.operation_id
        )
        self.assertEqual(captured.status, operation.status)
        self.assertIsNone(captured.recovery_journal)
        assert captured.disposable_replacement_preparation is not None
        self.assertEqual(
            captured.disposable_replacement_preparation.retained_target_id, target.target_id
        )
        self.assertTrue(captured.disposable_replacement_preparation.prepared_by)
        self.store.write_dokploy_target_id_record(
            target.model_copy(update={"target_id": "replacement-fixture"})
        )
        self.assertTrue(
            self.store.prepare_disposable_testing_replacement(captured, "another-observer")
        )
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(operation.operation_id),
            captured,
        )

    async def test_existing_director_grant_does_not_require_a_new_observation(self) -> None:
        from control_plane.service_auth import LocalOperatorPolicyRule

        policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            local_operators=(
                LocalOperatorPolicyRule(
                    subjects=("local-owner-agent",),
                    token_labels=("local-owner-write",),
                    products=("example-site",),
                    contexts=(self.context,),
                    instances=("testing",),
                    actions=("live_target_runtime.apply",),
                ),
            ),
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=policy,
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
        )
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
            return_value={},
        ) as driver:
            self.assertEqual(await self.sync(), 202)
            driver.assert_called_once()
            self.assertIsNone(driver.call_args.kwargs["before_provider_mutation"])

    async def test_testing_sync_on_live_product_gets_no_production_authority(self) -> None:
        record_safety(self.store, "example-site", self.context, "testing")
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
            return_value={"tracked_target": {"target_id": "target-testing"}},
        ) as driver:
            self.assertEqual(await self.sync(), 202)
            self.assertEqual(driver.call_args.kwargs["instance_name"], "testing")
            driver.reset_mock()
            self.assertEqual(await self.sync(instance="prod"), 403)
            driver.assert_not_called()

    async def test_owned_preview_runtime_permission_does_not_admit_a_forged_or_production_lane(
        self,
    ) -> None:
        from control_plane.contracts.preview_record import PreviewRecord
        from control_plane.live_target_runtime import (
            product_lane_declared_keys,
            LiveTargetRuntimeError,
        )

        profile = self.store.read_product_profile_record("example-site")
        self.context = profile.preview.context
        preview = PreviewRecord(
            preview_id="preview-fixture",
            context=self.context,
            anchor_repo=profile.repository,
            anchor_pr_number=7,
            anchor_pr_url="https://github.com/every/example-site/pull/7",
            preview_label="preview",
            canonical_url="https://pr-7.example.invalid",
            state="active",
            created_at=profile.updated_at,
            updated_at=profile.updated_at,
            eligible_at=profile.updated_at,
        )
        self.store.write_preview_record(preview)
        slug = profile.preview.slug_template.format(number=preview.anchor_pr_number)
        record_safety(self.store, profile.product, self.context, slug, preview=preview)
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
            return_value={},
        ) as driver:
            self.assertEqual(await self.sync(instance=slug), 202)
            driver.call_args.kwargs["before_provider_mutation"]()
            product_lane_declared_keys(
                record_store=self.store,
                product_name=profile.product,
                context_name=self.context,
                instance_name=slug,
            )
            driver.reset_mock()
            self.assertEqual(await self.sync(instance="prod"), 403)
            self.assertEqual(await self.sync(instance="pr-unowned"), 409)
            driver.assert_not_called()
            self.store.write_preview_record(
                preview.model_copy(update={"anchor_repo": "another/site"})
            )
            with self.assertRaises(LiveTargetRuntimeError):
                product_lane_declared_keys(
                    record_store=self.store,
                    product_name=profile.product,
                    context_name=self.context,
                    instance_name=slug,
                )

    async def test_completed_runtime_retry_does_not_need_current_mutation_permission(self) -> None:
        record_safety(self.store, "example-site", self.context, "testing")
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
            return_value={},
        ) as driver:
            self.assertEqual(await self.sync(), 202)
            profile = self.store.read_product_profile_record("example-site")
            self.store.write_product_profile_record(
                profile.model_copy(update={"disposable_lane_safety": ()})
            )
            self.assertEqual(await self.sync(), 202)
            driver.assert_called_once()

    async def test_client_identity_gets_no_new_permission(self) -> None:
        record_safety(self.store, "example-site", self.context, "testing")
        with (
            scoped_agent_transport(),
            patch(
                "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment"
            ) as driver,
        ):
            self.assertEqual(await self.sync(), 403)
            driver.assert_not_called()

    async def test_director_terminal_runtime_permission_is_testing_only_even_with_a_production_grant(
        self,
    ) -> None:
        from control_plane.service_auth import BearerIdentityConfig, TerminalAgentPolicyRule

        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(
                schema_version=2,
                terminal_agents=(
                    TerminalAgentPolicyRule(
                        subjects=("local-owner-agent",),
                        token_labels=("local-owner-write",),
                        products=("example-site",),
                        contexts=(self.context,),
                        instances=("prod",),
                        actions=("live_target_runtime.apply",),
                    ),
                ),
            ),
            bearer_identity_config=BearerIdentityConfig(
                terminal_agent_token="local-operator-token",
                terminal_agent_subject="local-owner-agent",
                terminal_agent_token_label="local-owner-write",
            ),
            record_store_factory=lambda: self.store,
        )
        record_safety(self.store, "example-site", self.context, "testing")
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
            return_value={},
        ) as driver:
            self.assertEqual(await self.sync(), 202)
            driver.reset_mock()
            self.assertEqual(await self.sync(instance="prod"), 403)
            driver.assert_not_called()

    async def test_protected_or_unknown_testing_never_reaches_the_driver(self) -> None:
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment"
        ) as driver:
            self.assertEqual(await self.sync(), 409)
            for changes in (
                {"usage": "in_use"},
                {"data": "only_copy"},
                {"production_resources": "shared"},
                {"usage": "unknown"},
                {"binding_sha256": "0" * 64},
            ):
                with self.subTest(changes=changes):
                    record_safety(self.store, "example-site", self.context, "testing", **changes)
                    self.assertEqual(await self.sync(), 409)
            now = datetime.now(timezone.utc)
            record_safety(
                self.store,
                "example-site",
                self.context,
                "testing",
                observed_at=now - timedelta(minutes=20),
                expires_at=now - timedelta(minutes=10),
            )
            self.assertEqual(await self.sync(), 409)
            driver.assert_not_called()

    async def test_production_target_alias_is_refused_even_with_an_isolation_claim(self) -> None:
        target_id = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        production = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="prod"
        )
        self.store.write_dokploy_target_id_record(
            target_id.model_copy(update={"target_id": production.target_id})
        )
        record_safety(self.store, "example-site", self.context, "testing")
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment"
        ) as driver:
            self.assertEqual(await self.sync(), 409)
            driver.assert_not_called()

    async def test_replacing_observation_does_not_change_an_inflight_apply_binding(self) -> None:
        from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord

        record_safety(self.store, "example-site", self.context, "testing")
        effects: list[str] = []

        def drift_then_write(**kwargs: object) -> dict[str, object]:
            self.store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context=self.context,
                    instance="testing",
                    env={"VOLUME_NAME": "new-binding"},
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            record_safety(self.store, "example-site", self.context, "testing")
            callback = kwargs["before_provider_mutation"]
            assert callable(callback)
            callback()
            effects.append("write")
            return {}

        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
            side_effect=drift_then_write,
        ):
            self.assertEqual(await self.sync(), 400)
        self.assertEqual(effects, [])

    async def test_separation_stages_new_resources_without_starting_shared_containers(self) -> None:
        record_safety(
            self.store,
            "example-site",
            self.context,
            "testing",
            production_resources="separation_ready",
        )
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
            return_value={},
        ) as driver:
            self.assertEqual(await self.sync(), 202)
            self.assertFalse(driver.call_args.kwargs["deploy"])
            driver.reset_mock()
            self.assertEqual(await self.sync(deploy=True), 409)
            driver.assert_not_called()
