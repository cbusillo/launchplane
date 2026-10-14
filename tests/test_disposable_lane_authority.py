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
        self.assertEqual(stored, initial)
        safety = self.store.read_disposable_lane_safety("example-site", self.context, "testing")
        assert safety is not None
        self.assertTrue(safety.observed_by)
        self.assertEqual(safety.observer_kind, "director_agent")
        self.assertEqual(safety.usage, "unknown")
        self.assertEqual(safety.data, "unknown")
        self.assertEqual(safety.production_resources, "unknown")
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment"
        ) as driver:
            self.assertEqual(await self.sync(), 409)
            driver.assert_not_called()
        with scoped_agent_transport():
            denied = await self.observe(mode="record", expected_binding_sha256=proof)
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(self.store.read_product_profile_record("example-site"), stored)
        denied = await self.observe(instance="prod")
        self.assertEqual(denied.status_code, 409, denied.text)
        self.assertEqual(self.store.read_product_profile_record("example-site"), stored)
        cleared = await self.observe(mode="clear", expected_binding_sha256=proof)
        self.assertEqual(cleared.status_code, 202, cleared.text)
        self.assertEqual(self.store.read_product_profile_record("example-site"), initial)

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
        safety = self.store.read_disposable_lane_safety("example-site", self.context, "testing")
        assert safety is not None
        forged["disposable_lane_safety"] = [safety.model_dump(mode="json")]
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
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.store.read_product_profile_record("example-site"), profile)
        with scoped_agent_transport():
            ordinary = await request(
                self.app,
                "POST",
                "/v1/product-profiles",
                headers={
                    "Authorization": "Bearer local-operator-token",
                    "Idempotency-Key": "ordinary-profile-write",
                },
                payload=profile.model_dump(mode="json"),
            )
        self.assertEqual(ordinary.status_code, 202, ordinary.text)
        self.assertEqual(
            self.store.read_disposable_lane_safety("example-site", self.context, "testing"), safety
        )

    async def test_collector_resolves_service_holds_and_exact_target_aliases(self) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold

        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        held = target.model_copy(deep=True)
        held.policies.staff_testing_hold = DokployTargetStaffTestingHold(
            reason="Staff are testing this lane.",
            recorded_at=target.updated_at,
            recorded_by="fixture-human",
        )
        self.store.write_dokploy_target_record(held)
        testing_id = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            testing_id.model_copy(update={"instance": "prod"})
        )
        inspected = await self.observe()
        collected = await self.observe(
            mode="collect",
            expected_binding_sha256=inspected.json()["result"]["binding_sha256"],
            usage="unused",
            data="reproducible",
            production_resources="isolated",
            usage_evidence="untrusted:unused",
            data_evidence="untrusted:copy",
            isolation_evidence="untrusted:isolated",
        )
        self.assertEqual(collected.status_code, 202, collected.text)
        evidence = self.store.read_disposable_lane_safety("example-site", self.context, "testing")
        assert evidence is not None
        self.assertEqual(evidence.usage, "in_use")
        self.assertEqual(evidence.production_resources, "shared")
        self.assertEqual(evidence.data, "unknown")
        self.assertEqual(evidence.observer_kind, "director_agent")
        self.assertNotIn("untrusted:", evidence.model_dump_json())
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment"
        ) as driver:
            self.assertEqual(await self.sync(), 409)
            driver.assert_not_called()

    async def test_collector_never_infers_unused_reproducible_or_isolated_from_absence(
        self,
    ) -> None:
        initial = self.store.read_product_profile_record("example-site")
        inspected = await self.observe()
        collected = await self.observe(
            mode="collect",
            expected_binding_sha256=inspected.json()["result"]["binding_sha256"],
            usage="unused",
            data="reproducible",
            production_resources="isolated",
        )
        self.assertEqual(collected.status_code, 202, collected.text)
        evidence = self.store.read_disposable_lane_safety("example-site", self.context, "testing")
        assert evidence is not None
        self.assertEqual(
            (evidence.usage, evidence.data, evidence.production_resources),
            ("unknown", "unknown", "unknown"),
        )
        self.assertEqual(self.store.read_product_profile_record("example-site"), initial)
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment"
        ) as driver:
            self.assertEqual(await self.sync(), 409)
            self.assertEqual(await self.sync(instance="prod"), 403)
            driver.assert_not_called()
        with scoped_agent_transport():
            refused = await self.observe(
                mode="collect", expected_binding_sha256=evidence.binding_sha256
            )
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(
            self.store.read_disposable_lane_safety("example-site", self.context, "testing"),
            evidence,
        )

    async def test_collect_refuses_changed_bindings_and_replacement_qualification(self) -> None:
        inspected = await self.observe()
        digest = inspected.json()["result"]["binding_sha256"]
        target_id = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target_id.model_copy(update={"target_id": "replacement"})
        )
        refused = await self.observe(mode="collect", expected_binding_sha256=digest)
        self.assertEqual(refused.status_code, 409)
        self.assertIsNone(
            self.store.read_disposable_lane_safety("example-site", self.context, "testing")
        )
        inspected = await self.observe()
        refused = await self.observe(
            mode="collect",
            expected_binding_sha256=inspected.json()["result"]["binding_sha256"],
            replaces_operation_id="retained-operation",
        )
        self.assertEqual(refused.status_code, 409)
        self.assertIsNone(
            self.store.read_disposable_lane_safety("example-site", self.context, "testing")
        )

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
        preparation = self.store.read_disposable_replacement_preparation(operation.operation_id)
        assert preparation is not None
        self.assertEqual(preparation.retained_target_id, target.target_id)
        self.assertTrue(preparation.prepared_by)
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

    async def test_existing_context_grant_cannot_bypass_protected_testing_safety(self) -> None:
        from control_plane.service_auth import LocalOperatorPolicyRule

        policy = LaunchplaneAuthzPolicy(
            schema_version=2,
            local_operators=(
                LocalOperatorPolicyRule(
                    subjects=("local-owner-agent",),
                    token_labels=("local-owner-write",),
                    products=("example-site",),
                    contexts=(self.context,),
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
            self.assertEqual(await self.sync(), 409)
            driver.assert_not_called()
            record_safety(self.store, "example-site", self.context, "testing")
            self.assertEqual(await self.sync(), 202)
            driver.assert_called_once()
            self.assertIsNotNone(driver.call_args.kwargs["before_provider_mutation"])
            driver.reset_mock()
            record_safety(self.store, "example-site", self.context, "testing", data="only_copy")
            self.assertEqual(await self.sync(deploy=True), 409)
            driver.assert_not_called()

    async def test_original_policy_scope_preserves_context_grants_and_does_not_activate_instance_grants(
        self,
    ) -> None:
        from control_plane.service_auth import LocalOperatorPolicyRule

        for instance_scope, scoped_client, expected in (
            (("prod",), False, 403),
            (("testing",), True, 403),
            ((), False, 202),
        ):
            with self.subTest(scope=instance_scope, client=scoped_client):
                policy = LaunchplaneAuthzPolicy(
                    schema_version=2,
                    local_operators=(
                        LocalOperatorPolicyRule(
                            subjects=("local-owner-agent",),
                            token_labels=("local-owner-write",),
                            products=("example-site",),
                            contexts=(self.context,),
                            instances=instance_scope,
                            actions=("live_target_runtime.apply",),
                        ),
                    ),
                )
                self.app = create_launchplane_fastapi_app(
                    verifier=_RejectingVerifier(),
                    authz_policy=policy,
                    bearer_identity_config=_local_operator_bearer_config(
                        token_label="local-owner-write"
                    ),
                    record_store_factory=lambda: self.store,
                )
                with patch(
                    "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
                    return_value={},
                ) as driver:
                    if scoped_client:
                        with scoped_agent_transport():
                            status = await self.sync()
                    else:
                        status = await self.sync(instance="prod")
                    self.assertEqual(status, expected)
                    self.assertEqual(driver.called, expected == 202)

    async def test_rebuild_stages_fresh_empty_resources_without_a_service_grant_and_retains_the_fence(
        self,
    ) -> None:
        from tests.support.disposable_lanes import prepare_held_rebuild
        from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope

        operation = prepare_held_rebuild(self.store, "example-site", self.context)
        current = self.store.read_provider_target_record(
            context_name=self.context, instance_name="testing"
        )
        payload = {
            "mode": "apply",
            "operation": "create-compose",
            "context": self.context,
            "instance": "testing",
            "server_id": "fixture-server",
            "project_name": "fixture-new-project",
            "environment_name": "fixture-new-environment",
            "target_name": "fixture-new-testing",
            "reason": "Stage an empty disposable replacement.",
            "replaces_operation_id": operation.operation_id,
            "expected_current_provider_target": current.to_deployed_target_reference().model_dump(
                mode="json"
            ),
        }
        envelope = DokployTargetSetupEnvelope.model_validate(payload)
        payload["confirmation"] = envelope.expected_confirmation
        posted: list[tuple[str, Any]] = []
        created = {
            "/api/project.create": {"projectId": "fixture-new-project-id"},
            "/api/environment.create": {"environmentId": "fixture-new-environment-id"},
            "/api/compose.create": {"composeId": "fixture-new-compose-id"},
        }

        def create(_host: str, _token: str, path: str, body: Any) -> Any:
            posted.append((path, body))
            if path not in created:
                raise AssertionError(f"Unexpected provider mutation: {path}")
            return created[path]

        async def stage() -> Any:
            return await request(
                self.app,
                "POST",
                "/v1/dokploy-targets/setup",
                headers={
                    "Authorization": "Bearer local-operator-token",
                    "Idempotency-Key": "empty-rebuild",
                },
                payload=payload,
            )

        with (
            patch(
                "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
                return_value=("https://fixture.invalid", "fixture-token"),
            ),
            patch(
                "control_plane.dokploy_target_setup_http.mutate_dokploy_payload_for_target_setup",
                side_effect=create,
            ),
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                return_value={
                    "composeId": created["/api/compose.create"]["composeId"],
                    "name": "fixture-new-testing",
                    "appName": "fixture-new-testing",
                    "serverId": "fixture-server",
                    "environmentId": created["/api/environment.create"]["environmentId"],
                    "env": "",
                    "composeFile": "",
                    "sourceType": "raw",
                },
            ),
        ):
            unknown = await stage()
            self.assertEqual(unknown.status_code, 409, unknown.text)
            self.assertEqual(unknown.json()["error"]["code"], "disposable_lane_safety_unknown")
            self.assertFalse(posted)
            # This fixture supplies server facts; the public assertion API
            # cannot manufacture them while the collector is unavailable.
            record_safety(self.store, "example-site", self.context, "testing")
            with scoped_agent_transport():
                client = await stage()
            self.assertEqual(client.status_code, 403, client.text)
            self.assertFalse(posted)
            applied = await stage()
            self.assertEqual(applied.status_code, 202, applied.text)
        self.assertEqual(
            [path for path, _ in posted],
            ["/api/project.create", "/api/environment.create", "/api/compose.create"],
        )
        self.assertEqual(posted[1][1]["projectId"], created["/api/project.create"]["projectId"])
        self.assertEqual(
            posted[2][1]["environmentId"], created["/api/environment.create"]["environmentId"]
        )
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(operation.operation_id),
            operation,
        )
        self.assertEqual(
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            ).target_id,
            created["/api/compose.create"]["composeId"],
        )

    async def test_rebuild_setup_cannot_mutate_production_or_reuse_placement(self) -> None:
        from tests.support.disposable_lanes import prepare_held_rebuild
        from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope

        operation = prepare_held_rebuild(self.store, "example-site", self.context)
        current = self.store.read_provider_target_record(
            context_name=self.context, instance_name="testing"
        )
        payload = {
            "mode": "apply",
            "operation": "create-compose",
            "context": self.context,
            "instance": "testing",
            "server_id": "fixture-server",
            "project_name": "fixture-project",
            "environment_name": "fixture-environment",
            "target_name": "fixture-testing",
            "reason": "Rebuild.",
            "replaces_operation_id": operation.operation_id,
            "expected_current_provider_target": current.to_deployed_target_reference().model_dump(
                mode="json"
            ),
        }
        for delta in (
            {"instance": "prod"},
            {"project_id": "production-project"},
            {"environment_id": "production-environment"},
            {"domains": ["live.example.invalid"]},
            {"custom_git_branch": "main"},
        ):
            with self.subTest(delta=delta):
                with self.assertRaises(ValueError):
                    DokployTargetSetupEnvelope.model_validate({**payload, **delta})
        payload.pop("replaces_operation_id")
        payload["instance"] = "prod"
        payload["confirmation"] = DokployTargetSetupEnvelope.model_validate(
            payload
        ).expected_confirmation
        with patch("control_plane.http_app.execute_dokploy_target_setup") as driver:
            response = await request(
                self.app,
                "POST",
                "/v1/dokploy-targets/setup",
                headers={
                    "Authorization": "Bearer local-operator-token",
                    "Idempotency-Key": "production-rebuild-denied",
                },
                payload=payload,
            )
            self.assertEqual(response.status_code, 403, response.text)
            driver.assert_not_called()

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
                allow_owned_preview=True,
            )
            with self.assertRaises(LiveTargetRuntimeError):
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
            self.store.clear_disposable_lane_safety("example-site", self.context, "testing")
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
            from control_plane.contracts.disposable_lane import MAX_DISPOSABLE_LANE_SAFETY_SECONDS

            record_safety(
                self.store,
                "example-site",
                self.context,
                "testing",
                observed_at=now - timedelta(seconds=MAX_DISPOSABLE_LANE_SAFETY_SECONDS),
                expires_at=now - timedelta(seconds=MAX_DISPOSABLE_LANE_SAFETY_SECONDS // 2),
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

    async def test_another_lanes_target_is_protected_even_with_positive_testing_facts(self) -> None:
        target = self.store.read_dokploy_target_id_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target.model_copy(update={"context": "other-product", "instance": "testing"})
        )
        record_safety(self.store, "example-site", self.context, "testing")
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment"
        ) as driver:
            self.assertEqual(await self.sync(), 409)
            driver.assert_not_called()

    async def test_global_configuration_drift_invalidates_testing_safety(self) -> None:
        from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord

        record = RuntimeEnvironmentRecord(
            scope="global",
            env={"DATABASE_HOST": "fixture-testing.invalid"},
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        self.store.write_runtime_environment_record(record)
        record_safety(self.store, "example-site", self.context, "testing")
        self.store.write_runtime_environment_record(
            record.model_copy(update={"env": {"DATABASE_HOST": "fixture-production.invalid"}})
        )
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment"
        ) as driver:
            self.assertEqual(await self.sync(), 409)
            driver.assert_not_called()

    async def test_granted_stable_lane_does_not_become_a_preview_by_context_collision(self) -> None:
        from control_plane.service_auth import LocalOperatorPolicyRule

        profile = self.store.read_product_profile_record("example-site")
        lane = next(lane for lane in profile.lanes if lane.instance == "testing")
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": (*profile.lanes, lane.model_copy(update={"instance": "staging"})),
                    "preview": profile.preview.model_copy(update={"context": self.context}),
                }
            )
        )
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(
                schema_version=2,
                local_operators=(
                    LocalOperatorPolicyRule(
                        subjects=("local-owner-agent",),
                        token_labels=("local-owner-write",),
                        products=("example-site",),
                        contexts=(self.context,),
                        actions=("live_target_runtime.apply",),
                    ),
                ),
            ),
            bearer_identity_config=_local_operator_bearer_config(token_label="local-owner-write"),
            record_store_factory=lambda: self.store,
        )
        with patch(
            "control_plane.http_app.control_plane_live_target_runtime.apply_live_target_runtime_environment",
            return_value={},
        ) as driver:
            self.assertEqual(await self.sync(instance="staging"), 202)
            self.assertIsNone(driver.call_args.kwargs["before_provider_mutation"])
            self.assertFalse(driver.call_args.kwargs["allow_owned_preview"])
            driver.reset_mock()
            with patch.object(
                self.store, "read_product_profile_record", side_effect=FileNotFoundError
            ):
                self.assertEqual(await self.sync(instance="another-stable"), 404)
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
