from copy import deepcopy
from control_plane.contracts.provider_target_origin import ProviderTargetOrigin

from contextlib import nullcontext
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


class _ApplicationStateVolumeFixture(_DirectorTestingSetupFixture):
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
                "previewDeployments": [],
                "ports": [],
                "security": [],
                "redirects": [],
                "domains": [],
                "replicas": 1,
                "applicationStatus": "idle",
                "env": None,
                "args": None,
                "networkIds": [],
                "detachDokployNetwork": False,
                "placementSwarm": None,
                "networkSwarm": None,
                "environment": {
                    "environmentId": "created-environment",
                    "projectId": "created-project",
                    "env": None,
                    "project": {"projectId": "created-project", "env": None},
                },
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

    async def _testing_hold(self, *, mode: str = "dry-run", **changes: Any) -> Any:
        from tests.support.http import request

        return await request(
            self.app,
            "POST",
            "/v1/product-config/testing-hold/apply",
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": "application-hold-" + ("set" if changes.get("hold") else "lift"),
            },
            payload={
                "product": self.profile.product,
                "context": self.context,
                "instance": "testing",
                "hold": False,
                "purpose": "isolation_staging",
                "reason": "Continue the isolated testing application through ordinary deployment.",
                "mode": mode,
                **changes,
            },
        )

    async def prepare_application_hold(self) -> None:
        from control_plane.http_app import launchplane_identity_actor
        from control_plane.service_auth import configured_local_operator_identity
        from control_plane.testing_lane_hold import (
            TestingHoldApplyRequest,
            apply_testing_hold_plan,
            build_testing_hold_plan,
        )
        from tests.http_app_test_support import _local_operator_bearer_config

        self.storage_inspector = self.enterContext(
            patch("control_plane.application_testing_storage.inspect_application_testing_storage")
        )

        # SQLite rehearses HTTP semantics; PostgreSQL owns real authority exclusion.
        if self.store.database_dialect_name != "postgresql":
            self.enterContext(
                patch.object(
                    self.store, "serialize_disposable_testing_resources", side_effect=nullcontext
                )
            )

        await self.plan()
        created = await self.post()
        self.assertEqual(created.status_code, 202, created.text)
        self.register_testing_lane()
        identity = configured_local_operator_identity(_local_operator_bearer_config())
        assert identity is not None
        actor = launchplane_identity_actor(identity)
        request = TestingHoldApplyRequest(
            product=self.profile.product,
            context=self.context,
            instance="testing",
            hold=True,
            purpose="isolation_staging",
            reason="Retain this allocation's staging interlock.",
        )
        plan, _ = build_testing_hold_plan(record_store=self.store, request=request, actor=actor)
        apply_testing_hold_plan(
            record_store=self.store,
            request=request.model_copy(
                update={"mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
            ),
            actor=actor,
        )

    async def _reviewed_application_lift(self) -> Any:
        plan = await self._testing_hold()
        self.assertEqual(plan.status_code, 202, plan.text)
        return await self._testing_hold(
            mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
        )

    def private_domain(self) -> dict[str, Any]:
        """Hydrated domain.one defaults for the supported private-origin creation."""
        return {
            "domainId": "testing-domain",
            "host": "testing.example.invalid",
            "path": "/",
            "internalPath": "/",
            "port": self.profile.runtime_port,
            "https": False,
            "applicationId": self.live["applicationId"],
            "certificateType": "none",
            "customCertResolver": None,
            "composeId": None,
            "serviceName": None,
            "domainType": "application",
            "previewDeploymentId": None,
            "stripPath": False,
            "customEntrypoint": None,
            "middlewares": [],
            "forwardAuthEnabled": False,
            "enabled": True,
        }


class ApplicationStateVolumeTests(_ApplicationStateVolumeFixture):
    async def test_application_lift_plan_remains_inert_when_provider_or_allocation_is_unavailable(
        self,
    ) -> None:
        await self.prepare_application_hold()
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(update={"project_name": "legacy-project"})
        )
        with (
            patch.object(
                self.store,
                "testing_replacement_execution_lock",
                side_effect=AssertionError("Planning acquired an execution lock"),
            ),
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                side_effect=AssertionError("Planning read the provider"),
            ),
        ):
            plan = await self._testing_hold()
        self.assertEqual(plan.status_code, 202, plan.text)
        self.assertEqual(plan.json()["result"]["action"], "clear")
        self.assertFalse(plan.json()["result"]["applied"])
        self.storage_inspector.assert_not_called()

    async def test_provider_transport_and_inventory_refusals_keep_hold_with_bounded_response(
        self,
    ) -> None:
        from http.client import IncompleteRead
        from urllib.error import URLError
        from control_plane.dokploy_inventory import InventoryUnavailable

        await self.prepare_application_hold()
        plan = await self._testing_hold()
        for error in (
            InventoryUnavailable("fixture-private-detail"),
            RuntimeError("fixture-private-detail"),
            URLError("fixture-private-detail"),
            IncompleteRead(b"fixture-private-detail"),
        ):
            with (
                self.subTest(error=type(error).__name__),
                patch(
                    "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                    side_effect=error,
                ),
            ):
                result = await self._testing_hold(
                    mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
                )
            self.assertEqual(result.status_code, 409, result.text)
            self.assertNotIn("fixture-private-detail", result.text)
            self.assertIsNotNone(
                self.store.read_dokploy_target_record(
                    context_name=self.context, instance_name="testing"
                ).policies.staff_testing_hold
            )

    async def test_original_application_allocation_can_lift_its_own_staging_hold(self) -> None:
        await self.prepare_application_hold()
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(update={"domains": ("testing.example.invalid",)})
        )
        self.live["domains"] = [self.private_domain()]
        provider_calls = self.provider.call_count
        plan = await self._testing_hold()
        self.assertEqual(plan.status_code, 202, plan.text)
        self.storage_inspector.assert_not_called()
        reviewed = plan.json()["result"]["plan_sha256"]
        applied = await self._testing_hold(mode="apply", reviewed_plan_sha256=reviewed)
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertTrue(applied.json()["result"]["read_back_matches"])
        self.assertFalse(applied.json()["result"]["reconcile_requested"])
        self.assertEqual(applied.json()["records"], plan.json()["records"])
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.assertIsNone(target.policies.staff_testing_hold)
        self.assertEqual(self.store.list_product_reconcile_requests(), ())
        # An exact replay reports the original result, even after provider drift.
        self.live["dockerImage"] = "fixture.invalid/example:changed-after-lift"
        replay = await self._testing_hold(mode="apply", reviewed_plan_sha256=reviewed)
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertEqual(replay.json()["result"], applied.json()["result"])
        self.assertEqual(self.provider.call_count, provider_calls)
        self.assertEqual(self.storage_inspector.call_count, 1)
        self.assert_production_unchanged()

    async def test_application_continuation_refuses_live_domain_drift(self) -> None:
        await self.prepare_application_hold()
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(update={"domains": ("testing.example.invalid",)})
        )
        domain = self.private_domain()
        plan = await self._testing_hold()
        variations: tuple[object, ...] = (
            None,
            {},
            [{"host": domain["host"]}],
            [domain, {**domain, "domainId": "second-domain", "host": "second.example.invalid"}],
            *(
                [{**domain, **changes}]
                for changes in (
                    {"host": "production.example.invalid"},
                    {"host": "*.example.invalid"},
                    {"path": "/production-path"},
                    {"internalPath": "/another-path"},
                    {"https": True},
                    {"certificateType": "letsencrypt"},
                    {"customCertResolver": "foreign-resolver"},
                    {"applicationId": "another-application"},
                    {"port": self.profile.runtime_port + 1},
                    {"serviceName": "another-service"},
                    {"composeId": "another-compose"},
                    {"previewDeploymentId": "another-preview"},
                    {"stripPath": True},
                    {"domainType": "compose"},
                    {"middlewares": ["unreviewed-middleware"]},
                    {"customEntrypoint": "websecure"},
                    {"forwardAuthEnabled": True},
                    {"enabled": False},
                )
            ),
        )
        for domains in variations:
            with self.subTest(domains=domains):
                self.live["domains"] = domains
                result = await self._testing_hold(
                    mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
                )
                self.assertEqual(result.status_code, 409, result.text)
                self.assertIsNotNone(
                    self.store.read_dokploy_target_record(
                        context_name=self.context, instance_name="testing"
                    ).policies.staff_testing_hold
                )
        self.storage_inspector.assert_not_called()
        self.assertEqual(self.store.list_product_reconcile_requests(), ())
        self.assert_production_unchanged()

    async def test_application_continuation_refuses_another_lanes_hostname_claim(self) -> None:
        from control_plane.contracts.dokploy_target_record import PublicHostEffect

        await self.prepare_application_hold()
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        hostname = "testing.example.invalid"
        self.store.write_dokploy_target_record(target.model_copy(update={"domains": (hostname,)}))
        self.live["domains"] = [self.private_domain()]
        plan = await self._testing_hold()
        targets = self.store.list_dokploy_target_records()
        unrelated = self.production.model_copy(
            update={"domains": ("https://unrelated.example.invalid/path",)}
        )
        with patch.object(
            self.store, "list_dokploy_target_records", return_value=(*targets, unrelated)
        ):
            allowed = await self._testing_hold()
        self.assertEqual(allowed.status_code, 202, allowed.text)
        for changes in (
            {"domains": (hostname,)},
            {"domains": ("TESTING.EXAMPLE.INVALID.",)},
            {"domains": (f"https://{hostname}/",)},
            {"domains": (f"https://{hostname}/path",)},
            {"domains": ("*.example.invalid",)},
            {"domains": ("https://*.example.invalid/",)},
            {"domains": ("~^testing[.]example[.]invalid$",)},
            {"domains": ("https://",)},
            {"domains": ("https://[invalid",)},
            {"public_hosts": (hostname,)},
            {
                "pending_public_host_effects": (
                    PublicHostEffect(
                        host=hostname,
                        provider_host="https://provider.example.invalid",
                        target_id=self.production_id.target_id,
                        runtime_port=self.profile.runtime_port,
                    ),
                )
            },
        ):
            with self.subTest(changes=changes):
                other = self.production.model_copy(update=changes)
                with patch.object(
                    self.store, "list_dokploy_target_records", return_value=(*targets, other)
                ):
                    result = await self._testing_hold(
                        mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
                    )
                self.assertEqual(result.status_code, 409, result.text)
                self.assertIsNotNone(
                    self.store.read_dokploy_target_record(
                        context_name=self.context, instance_name="testing"
                    ).policies.staff_testing_hold
                )
        self.storage_inspector.assert_not_called()
        self.assert_production_unchanged()

    async def test_application_continuation_refuses_live_source_mount_placement_and_execution_drift(
        self,
    ) -> None:
        await self.prepare_application_hold()
        original = deepcopy(self.live)
        plan = await self._testing_hold()
        reviewed = plan.json()["result"]["plan_sha256"]
        calls = self.provider.call_count
        variations: tuple[dict[str, Any], ...] = (
            {"repository": "some-other-source"},
            {"sourceType": "unsupported"},
            {"dockerImage": "fixture.invalid/example:configured"},
            {"env": "UNTRACKED_KEY=fixture-only"},
            {"username": "unreviewed-registry-user"},
            {"password": "fixture-secret"},
            {"registryId": "unreviewed-registry"},
            {"registryUrl": "https://registry.example.invalid"},
            {"command": "unreviewed-command"},
            {"args": ["unreviewed-argument"]},
            {"buildServerId": "another-server"},
            {"buildRegistryId": "unreviewed-registry"},
            {"rollbackRegistryId": "unreviewed-registry"},
            {"registry": {"registryId": "unreviewed-registry"}},
            {"ports": [{"publishedPort": 8080, "targetPort": 8080}]},
            {"ports": None},
            {"networkSwarm": [{"Target": "production-overlay"}]},
            {"networkSwarm": []},
            {"networkIds": ["production-network"]},
            {"detachDokployNetwork": True},
            {"placementSwarm": {"Constraints": ["node.labels.production==true"]}},
            {"placementSwarm": {}},
            {"modeSwarm": {"Global": {}}},
            {"labelsSwarm": {"traefik.enable": "true"}},
            {"healthCheckSwarm": {"Test": ["CMD", "unreviewed-command"]}},
            {"endpointSpecSwarm": {"Ports": [{"PublishedPort": 8080}]}},
            {"security": [{"username": "unreviewed-user"}]},
            {"redirects": [{"regex": ".*", "replacement": "https://elsewhere.invalid"}]},
            {"replicas": 2},
            {"replicas": None},
            {"memoryLimit": "1024"},
            {"previewDeployments": [{"previewDeploymentId": "already-started"}]},
            {"mounts": []},
            {"mounts": [{**original["mounts"][0], "volumeName": "foreign-state"}]},
            {"serverId": "another-server"},
            {"deployments": [{"deploymentId": "already-started"}]},
            {"applicationStatus": "running"},
        )
        for changes in variations:
            with self.subTest(changes=changes):
                self.live = {**deepcopy(original), **changes}
                result = await self._testing_hold(mode="apply", reviewed_plan_sha256=reviewed)
                self.assertEqual(result.status_code, 409, result.text)
                self.assertIsNotNone(
                    self.store.read_dokploy_target_record(
                        context_name=self.context, instance_name="testing"
                    ).policies.staff_testing_hold
                )
        self.assertEqual(self.provider.call_count, calls)
        self.storage_inspector.assert_not_called()
        self.assertEqual(self.store.list_product_reconcile_requests(), ())
        self.assert_production_unchanged()

    async def test_application_continuation_requires_original_empty_parent_environments(
        self,
    ) -> None:
        await self.prepare_application_hold()
        original = deepcopy(self.live)
        plan = await self._testing_hold()
        for environment in (
            None,
            {},
            {**original["environment"], "environmentId": "another-environment"},
            {**original["environment"], "projectId": "another-project"},
            {**original["environment"], "project": {"projectId": "another-project"}},
            {**original["environment"], "project": {"projectId": "created-project"}},
            {**original["environment"], "env": "UNTRACKED=fixture"},
            {
                **original["environment"],
                "project": {"projectId": "created-project", "env": "UNTRACKED=fixture"},
            },
        ):
            with self.subTest(environment=environment):
                self.live = {**deepcopy(original), "environment": environment}
                result = await self._testing_hold(
                    mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
                )
                self.assertEqual(result.status_code, 409, result.text)
                self.assertIsNotNone(
                    self.store.read_dokploy_target_record(
                        context_name=self.context, instance_name="testing"
                    ).policies.staff_testing_hold
                )
        self.storage_inspector.assert_not_called()
        self.assertEqual(self.store.list_product_reconcile_requests(), ())
        self.assert_production_unchanged()

    async def test_inert_source_build_settings_do_not_block_docker_runtime_continuation(
        self,
    ) -> None:
        await self.prepare_application_hold()
        self.live.update(
            dockerfile="unused.Dockerfile",
            buildPath="/unused",
            customGitBuildPath="/unused",
            dockerContextPath="/unused",
            dockerBuildStage="unused-stage",
            buildArgs="UNUSED=fixture",
            buildSecrets="UNUSED=fixture",
            args=[],
        )
        result = await self._reviewed_application_lift()
        self.assertEqual(result.status_code, 202, result.text)
        self.assertIsNone(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).policies.staff_testing_hold
        )
        self.storage_inspector.assert_called_once()
        self.assert_production_unchanged()

    async def test_late_cross_lane_idempotency_collision_retains_hold_and_original_receipt(
        self,
    ) -> None:
        from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
        from control_plane.http_app import idempotency_request_fingerprint
        from control_plane.http_routes.mutation_support import idempotency_scope
        from control_plane.service_auth import configured_local_operator_identity
        from control_plane.testing_lane_hold import TESTING_HOLD_APPLY_ROUTE
        from control_plane.workflows.ship import utc_now_timestamp
        from tests.http_app_test_support import _local_operator_bearer_config

        await self.prepare_application_hold()
        identity = configured_local_operator_identity(_local_operator_bearer_config())
        assert identity is not None
        # Another lane has a different execution lock but shares the caller's
        # route/key namespace. Its transaction can finish after our replay read.
        other_payload: dict[str, object] = {"context": "another-testing-context", "hold": False}
        other_receipt = LaunchplaneIdempotencyRecord(
            record_id="late-other-lane-receipt",
            scope=idempotency_scope(identity),
            route_path=TESTING_HOLD_APPLY_ROUTE,
            idempotency_key="application-hold-lift",
            request_fingerprint=idempotency_request_fingerprint(
                route_path=TESTING_HOLD_APPLY_ROUTE, payload=other_payload
            ),
            response_status_code=202,
            response_trace_id="other-lane-trace",
            recorded_at=utc_now_timestamp(),
            response_payload={"records": {"context": other_payload["context"]}},
        )
        self.storage_inspector.side_effect = lambda *_args, **_kwargs: (
            self.store.write_idempotency_record(other_receipt)
        )
        result = await self._reviewed_application_lift()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["error"]["code"], "idempotency_key_reused")
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=other_receipt.scope,
                route_path=other_receipt.route_path,
                idempotency_key=other_receipt.idempotency_key,
            ),
            other_receipt,
        )
        self.assertIsNotNone(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).policies.staff_testing_hold
        )
        self.assertEqual(self.store.list_product_reconcile_requests(), ())
        self.assert_production_unchanged()

    async def test_missing_physical_storage_proof_keeps_application_held(self) -> None:
        await self.prepare_application_hold()
        plan = await self._testing_hold()
        self.storage_inspector.side_effect = ValueError("fixture host proof unavailable")
        result = await self._testing_hold(
            mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
        )
        self.assertEqual(result.status_code, 409, result.text)
        self.assertIsNotNone(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).policies.staff_testing_hold
        )
        self.assertEqual(self.store.list_product_reconcile_requests(), ())
        self.assert_production_unchanged()

    async def test_application_host_inspector_requires_the_exact_bound_schedule_evidence(
        self,
    ) -> None:
        from control_plane.application_state_volume import ApplicationStateVolumeAdapter
        from control_plane.application_testing_storage import inspect_application_testing_storage
        from control_plane.http_routes.mutation_support import idempotency_scope
        from control_plane.service_auth import configured_local_operator_identity
        from tests.http_app_test_support import _local_operator_bearer_config

        await self.prepare_application_hold()
        identity = configured_local_operator_identity(_local_operator_bearer_config())
        assert identity is not None
        adapter = ApplicationStateVolumeAdapter.completed_for_testing_lane(
            self.root,
            self.store,
            product=self.profile.product,
            context=self.context,
            scope=idempotency_scope(identity),
            trace="fixture-inspection",
        )
        for evidence in (
            {},
            {"testing_resources_ok": "true", "testing_resources_binding_sha256": "b" * 64},
        ):
            with (
                self.subTest(evidence=evidence),
                patch(
                    "control_plane.application_testing_storage.post_deploy.run_manual_data_schedule",
                    return_value=evidence,
                ),
            ):
                with self.assertRaises(ValueError):
                    inspect_application_testing_storage(adapter, checkpoint=lambda _phase: None)

    async def test_replacement_allocations_reuse_the_lanes_inspection_slot(self) -> None:
        from dataclasses import replace
        from uuid import uuid4
        from control_plane.application_state_volume import (
            ApplicationStateVolumeAdapter,
            state_volume_allocation_names,
        )
        from control_plane.application_testing_storage import inspect_application_testing_storage
        from control_plane.http_routes.mutation_support import idempotency_scope
        from control_plane.service_auth import configured_local_operator_identity
        from tests.http_app_test_support import _local_operator_bearer_config

        await self.prepare_application_hold()
        identity = configured_local_operator_identity(_local_operator_bearer_config())
        assert identity is not None
        original = ApplicationStateVolumeAdapter.completed_for_testing_lane(
            self.root,
            self.store,
            product=self.profile.product,
            context=self.context,
            scope=idempotency_scope(identity),
            trace="fixture-inspection",
        )
        next_plan = uuid4().hex
        replacement_names = state_volume_allocation_names(next_plan)
        replacement = replace(
            original,
            intent={
                **original.intent,
                **replacement_names,
                "plan_id": next_plan,
            },
        )
        # The inspector consumes a previously qualified allocation. Model the
        # replacement's different physical identity without another HTTP fixture.
        next_phase = {
            "application": "replacement-application",
            "environment": "replacement-environment",
            "app_name": replacement_names["app_name"],
        }
        with patch(
            "control_plane.application_testing_storage.post_deploy.run_manual_data_schedule",
            return_value={},
        ) as dispatch:
            with self.assertRaises(ValueError):
                inspect_application_testing_storage(original, checkpoint=lambda _phase: None)
            with (
                patch.object(replacement, "_progress_phase", return_value=json.dumps(next_phase)),
                patch.object(
                    replacement,
                    "_live",
                    return_value={
                        "serverId": self.payload["server_id"],
                        "appName": next_phase["app_name"],
                    },
                ),
                self.assertRaises(ValueError),
            ):
                inspect_application_testing_storage(replacement, checkpoint=lambda _phase: None)
        self.assertEqual(
            dispatch.call_args_list[0].kwargs["schedule_app_name"],
            dispatch.call_args_list[1].kwargs["schedule_app_name"],
        )

    async def test_another_scope_cannot_lift_the_original_allocations_hold(self) -> None:
        from tests.http_app_test_support import _local_operator_bearer_config

        await self.prepare_application_hold()
        self.app = self.make_app(_local_operator_bearer_config(token_label="another-token"))
        result = await self._reviewed_application_lift()
        self.assertEqual(result.status_code, 409, result.text)
        self.storage_inspector.assert_not_called()
        self.assertIsNotNone(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).policies.staff_testing_hold
        )

    async def test_application_alias_and_unknown_provider_effect_preserve_hold(self) -> None:
        from control_plane.workflows.generic_web_deploy_provider import (
            dokploy_generic_web_provider_target_key,
        )

        await self.prepare_application_hold()
        current = self.store.read_provider_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.store.write_provider_target_record(
            self.production_provider.model_copy(update={"target_id": current.target_id})
        )
        aliased = await self._reviewed_application_lift()
        self.assertEqual(aliased.status_code, 409, aliased.text)
        self.store.write_provider_target_record(self.production_provider)
        reservation = self.store.reserve_mutation(
            scope="original-caller",
            route_path="/original-operation",
            idempotency_key="original-key",
            request_fingerprint="original-request",
            provider_target_key=dokploy_generic_web_provider_target_key(current),
            provider_target_origin=ProviderTargetOrigin(
                product="example-site",
                context=current.context,
                instance=current.instance,
                provider_id=current.provider_id,
                target_type=current.target_category,
                target_id=current.target_id,
            ),
            reconciliation_key="original-binding",
            lease_owner="original-worker",
        )
        assert reservation.record is not None
        self.store.mark_mutation_reconcile_required(
            reservation=reservation.record,
            reconciliation_key="original-binding",
        )
        unknown = await self._reviewed_application_lift()
        self.assertEqual(unknown.status_code, 409, unknown.text)
        self.storage_inspector.assert_not_called()
        self.assertIsNotNone(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).policies.staff_testing_hold
        )
        self.assertEqual(self.store.list_product_reconcile_requests(), ())
        self.assert_production_unchanged()

    async def test_application_continuation_preserves_foreign_staff_and_protected_data(
        self,
    ) -> None:
        from tests.support.disposable_lanes import record_safety

        await self.prepare_application_hold()
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        hold = target.policies.staff_testing_hold
        assert hold is not None
        for changes in ({"recorded_by": "another-operator"}, {"purpose": "staff_testing"}):
            with self.subTest(hold=changes):
                changed = hold.model_copy(update=changes)
                self.store.write_dokploy_target_record(
                    target.model_copy(
                        update={
                            "policies": target.policies.model_copy(
                                update={"staff_testing_hold": changed}
                            )
                        }
                    )
                )
                result = (
                    await self._testing_hold()
                    if changed.purpose == "staff_testing"
                    else await self._reviewed_application_lift()
                )
                self.assertEqual(result.status_code, 409, result.text)
                self.assertEqual(
                    self.store.read_dokploy_target_record(
                        context_name=self.context, instance_name="testing"
                    ).policies.staff_testing_hold,
                    changed,
                )
        self.store.write_dokploy_target_record(target)
        for facts in (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"production_resources": "shared"},
        ):
            with self.subTest(facts=facts):
                record_safety(self.store, self.profile.product, self.context, "testing", **facts)
                result = await self._reviewed_application_lift()
                self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.store.list_product_reconcile_requests(), ())
        self.assert_production_unchanged()

    async def test_application_continuation_rechecks_ownership_under_authority_lock(self) -> None:
        from contextlib import contextmanager

        await self.prepare_application_hold()
        plan = await self._testing_hold()
        target = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        hold = target.policies.staff_testing_hold
        assert hold is not None
        protected = hold.model_copy(update={"purpose": "staff_testing", "recorded_by": "staff"})
        original_lock = self.store.serialize_disposable_testing_resources

        @contextmanager
        def changed_before_commit() -> Any:
            self.store.write_dokploy_target_record(
                target.model_copy(
                    update={
                        "policies": target.policies.model_copy(
                            update={"staff_testing_hold": protected}
                        )
                    }
                )
            )
            with original_lock():
                yield

        with patch.object(
            self.store, "serialize_disposable_testing_resources", side_effect=changed_before_commit
        ):
            result = await self._testing_hold(
                mode="apply", reviewed_plan_sha256=plan.json()["result"]["plan_sha256"]
            )
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).policies.staff_testing_hold,
            protected,
        )
        self.assertEqual(self.store.list_product_reconcile_requests(), ())

    async def test_compose_replacement_reads_source_outside_authority_commit(self) -> None:
        provider = self.register_unused_testing_compose(source_type="github")
        self.payload.update(
            expected_current_provider_target=provider.to_deployed_target_reference().model_dump(
                mode="json"
            )
        )
        writing_authority = False
        write_bundle = self.store._write_product_authority_bundle

        def write(*args: Any, **kwargs: Any) -> None:
            nonlocal writing_authority
            writing_authority = True
            try:
                write_bundle(*args, **kwargs)
            finally:
                writing_authority = False

        def read(_host: str, _token: str, target_type: str, target_id: str) -> dict[str, Any]:
            self.assertFalse(writing_authority, "Provider I/O must stay outside authority commit")
            if target_type == "compose":
                return {
                    "composeId": target_id,
                    "environmentId": "old-environment",
                    "sourceType": "github",
                    "composePath": "./docker-compose.yml",
                }
            return deepcopy(self.live)

        with (
            patch.object(self.store, "_write_product_authority_bundle", side_effect=write),
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                side_effect=read,
            ),
        ):
            await self.plan()
            result = await self.post()
        self.assertEqual(result.status_code, 202, result.text)
        self.assertTrue(result.json()["result"]["applied"])
        self.assert_production_unchanged()

    async def test_original_plan_without_browser_field_still_applies_and_reads_back(self) -> None:
        from control_plane.application_state_volume import PLAN_ROUTE
        from control_plane.http_routes.mutation_support import (
            idempotency_scope,
            request_fingerprint,
        )
        from control_plane.service_auth import configured_local_operator_identity
        from tests.http_app_test_support import _local_operator_bearer_config

        plan = await self.plan()
        identity = configured_local_operator_identity(_local_operator_bearer_config())
        assert identity is not None
        original = self.store.read_idempotency_record(
            scope=idempotency_scope(identity),
            route_path=PLAN_ROUTE,
            idempotency_key=plan["state_volume_plan_id"],
        )
        self.assertIsNotNone(original)
        assert original is not None
        # Model an immutable pre-upgrade plan, whose request predates the browser adapter.
        old_intent = deepcopy(original.response_payload)
        old_intent["request"].pop("browser_review", None)
        old_intent.pop("plan_sha256")
        old_intent["plan_sha256"] = request_fingerprint(old_intent)
        self.store.write_idempotency_record(
            original.model_copy(
                update={
                    "request_fingerprint": old_intent["plan_sha256"],
                    "response_payload": old_intent,
                }
            )
        )
        self.payload["expected_plan_sha256"] = old_intent["plan_sha256"]
        applied = await self.post()
        self.assertEqual(applied.status_code, 202, applied.text)
        readback = await self.post(mode="readback")
        self.assertEqual(readback.status_code, 202, readback.text)
        self.assertTrue(readback.json()["result"]["configuration_matches"], readback.text)
        self.assert_production_unchanged()

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

    async def assert_source_drift_preserves_allocation(self, after_path: str) -> None:
        provider = self.register_unused_testing_compose(source_type="github")
        self.payload.update(**self.expected(provider))
        source: dict[str, Any] = {
            "composeId": provider.target_id,
            "environmentId": "old-environment",
            "sourceType": "github",
            "composePath": "./docker-compose.yml",
        }

        def mutate(host: str, token: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
            response = self.mutate(host, token, path, payload)
            if path == after_path:
                source["repository"] = "configured-during-allocation"
            return response

        def read(_host: str, _token: str, target_type: str, _target_id: str) -> dict[str, Any]:
            return deepcopy(source if target_type == "compose" else self.live)

        self.provider.side_effect = mutate
        with patch(
            "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
            side_effect=read,
        ):
            await self.plan()
            result = await self.post()
            self.assertEqual(result.status_code, 409, result.text)
            self.assertEqual(result.json()["error"]["code"], "mutation_reconciliation_required")
            paths = [call.args[2] for call in self.provider.call_args_list]
            self.assertEqual(paths[-1], after_path)
            self.assertEqual(len(paths), len(set(paths)))
            self.assertEqual(self.current_testing_target_id(), provider.target_id)
            self.assertTrue(self.store.list_held_provider_target_reservations())
            readback = (await self.post(mode="readback")).json()["result"]
            phase = json.loads(readback["provider_effect_phase"])
            self.assertEqual(phase["project"], self.placement["project_id"])
            if self.live:
                self.assertEqual(phase["application"], self.live["applicationId"])
            if after_path == MOUNT_CREATE:
                self.assertEqual(phase["mount_verified"], "true")
            calls = self.provider.call_count
            retry = await self.post()
            self.assertEqual(retry.status_code, 409, retry.text)
            self.assertEqual(self.provider.call_count, calls)
        self.assert_production_unchanged()

    async def test_source_drift_after_project_keeps_custody_before_next_create(self) -> None:
        await self.assert_source_drift_preserves_allocation(PROJECT_CREATE_PATH)

    async def test_source_drift_after_application_keeps_custody_before_mount(self) -> None:
        await self.assert_source_drift_preserves_allocation(APPLICATION_CREATE_PATH)

    async def test_source_drift_after_mount_keeps_custody_before_adoption(self) -> None:
        await self.assert_source_drift_preserves_allocation(MOUNT_CREATE)

    async def test_binding_drift_after_project_retains_returned_identity(self) -> None:
        await self.plan()
        changed = self.profile.model_copy(update={"display_name": "Concurrent metadata change"})
        acknowledged: dict[str, Any] = {}

        def create_then_change(
            host: str, token: str, path: str, payload: dict[str, Any]
        ) -> dict[str, Any]:
            response = self.mutate(host, token, path, payload)
            if path == PROJECT_CREATE_PATH:
                acknowledged.update(response)
                self.store.write_product_profile_record(changed)
            return response

        self.provider.side_effect = create_then_change
        result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        readback = (await self.post(mode="readback")).json()["result"]
        phase = json.loads(readback["provider_effect_phase"])
        self.assertEqual(phase["project"], self.placement["project_id"])
        self.assertEqual(phase["default_environment"], acknowledged["environment"]["environmentId"])
        self.assertEqual(self.provider.call_args_list[-1].args[2], PROJECT_CREATE_PATH)
        self.profile = changed
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
            self.assertFalse(retained.json()["result"]["applied"])
            replay = await self.post(
                retain_state_volume_allocation=True,
                expected_state_volume_custody_sha256=readback["custody_sha256"],
            )
            self.assertEqual(replay.status_code, 202, replay.text)
            old_apply = await self.post()
            self.assertEqual(old_apply.json()["result"]["allocation_state"], "retained_uncertain")
            self.assertFalse(old_apply.json()["result"]["applied"])
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

    async def test_completed_plan_under_new_key_cannot_hold_the_lane(self) -> None:
        await self.plan()
        success = await self.post()
        self.assertEqual(success.status_code, 202, success.text)
        self.placement = {}  # Original application later disappears from inventory.
        calls = self.provider.call_count
        retry = await self.post(idempotency_key="regenerated-key")
        self.assertEqual(retry.status_code, 409, retry.text)
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())
        self.assertEqual(self.provider.call_count, calls)
        replay = await self.post()
        self.assertEqual(replay.status_code, 202, replay.text)

    async def test_profile_metadata_change_does_not_prevent_exact_custody_retention(self) -> None:
        from datetime import timedelta
        from control_plane.contracts.idempotency_record import (
            format_launchplane_mutation_timestamp,
            parse_launchplane_mutation_timestamp,
        )

        await self.plan()
        self.fail_before = PROJECT_CREATE_PATH
        uncertain = await self.post()
        self.assertEqual(uncertain.status_code, 409, uncertain.text)
        updated = self.profile.model_copy(update={"display_name": "Renamed product"})
        self.store.write_product_profile_record(updated)
        readback = (await self.post(mode="readback")).json()["result"]
        row = self.store.list_held_provider_target_reservations()[0]
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
            self.assertFalse(retained.json()["result"]["applied"])
            fresh = await self.post(
                mode="dry-run", state_volume_plan_id="", expected_plan_sha256=""
            )
            self.assertEqual(fresh.status_code, 202, fresh.text)
        self.assertEqual(self.store.list_held_provider_target_reservations(), ())
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), updated)

    async def test_malformed_provider_success_is_unavailable_but_known_mount_difference_is_differs(
        self,
    ) -> None:
        await self.plan()
        success = await self.post()
        self.assertEqual(success.status_code, 202, success.text)
        calls = self.provider.call_count
        for invalid in (
            {},
            {"raw": "private provider details"},
            {"applicationId": "another-target"},
        ):
            with patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                return_value=invalid,
            ):
                readback = await self.post(mode="readback")
            self.assertEqual(readback.status_code, 202, readback.text)
            self.assertEqual(readback.json()["result"]["configuration_evidence"], "unavailable")
            self.assertFalse(readback.json()["result"]["configuration_matches"])
            self.assertNotIn("private provider details", readback.text)
        self.live["mounts"][0]["volumeName"] = "changed-volume"
        changed = await self.post(mode="readback")
        self.assertEqual(changed.json()["result"]["configuration_evidence"], "differs")
        self.assertEqual(self.provider.call_count, calls)

    async def test_retained_worker_cannot_publish_its_target(self) -> None:
        from datetime import timedelta
        from control_plane.application_state_volume_retention import (
            retain_state_volume_allocation,
            state_volume_custody_sha256,
        )
        from control_plane.contracts.idempotency_record import (
            format_launchplane_mutation_timestamp,
            parse_launchplane_mutation_timestamp,
        )

        await self.plan()
        write = self.store.write_product_authority_bundle
        retained = False

        def retain_before_commit(bundle: Any) -> None:
            nonlocal retained
            if not retained:
                retained = True
                row = self.store.list_held_provider_target_reservations()[0]
                now = format_launchplane_mutation_timestamp(
                    parse_launchplane_mutation_timestamp(
                        row.lease_expires_at, field_name="lease_expires_at"
                    )
                    + timedelta(seconds=1)
                )
                # Model failed outer executor exclusion: the final transaction
                # must independently refuse a worker whose custody was retained.
                with patch.object(self.store, "_database_mutation_timestamp", return_value=now):
                    retain_state_volume_allocation(
                        self.store,
                        reservation=row,
                        expected_custody_sha256=state_volume_custody_sha256(self.store, row),
                        expected_original_intent=self.store.read_original_mutation_intent(row),
                        trace="fixture-retention",
                        checkpoint=lambda: None,
                    )
            write(bundle)

        with patch.object(
            self.store, "write_product_authority_bundle", side_effect=retain_before_commit
        ):
            await self.post()
        self.assertTrue(retained)
        with self.assertRaises(FileNotFoundError):
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            )
        self.assert_production_unchanged()

    async def test_lease_expiry_at_record_commit_refuses_then_original_key_recovers(self) -> None:
        from datetime import timedelta
        from control_plane.contracts.idempotency_record import (
            format_launchplane_mutation_timestamp,
            parse_launchplane_mutation_timestamp,
        )

        await self.plan()
        write = self.store.write_product_authority_bundle

        def expire_before_commit(bundle: Any) -> None:
            row = self.store.list_held_provider_target_reservations()[0]
            now = format_launchplane_mutation_timestamp(
                parse_launchplane_mutation_timestamp(
                    row.lease_expires_at, field_name="lease_expires_at"
                )
                + timedelta(seconds=1)
            )
            with patch.object(self.store, "_database_mutation_timestamp", return_value=now):
                write(bundle)

        with patch.object(
            self.store, "write_product_authority_bundle", side_effect=expire_before_commit
        ):
            expired = await self.post()
        self.assertEqual(expired.status_code, 409, expired.text)
        with self.assertRaises(FileNotFoundError):
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            )
        calls = self.provider.call_count
        retry = await self.post()
        self.assertEqual(retry.status_code, 202, retry.text)
        self.assertEqual(self.provider.call_count, calls)

    async def test_already_adopted_allocation_recovers_its_receipt_instead_of_retaining(
        self,
    ) -> None:
        from datetime import timedelta
        from control_plane.contracts.idempotency_record import (
            format_launchplane_mutation_timestamp,
            parse_launchplane_mutation_timestamp,
        )

        await self.plan()
        with patch(
            "control_plane.application_state_volume.ApplicationStateVolumeAdapter._verify_adopted",
            side_effect=OSError("Fixture: interrupted after adoption"),
        ):
            uncertain = await self.post()
        self.assertEqual(uncertain.status_code, 409, uncertain.text)
        row = self.store.list_held_provider_target_reservations()[0]
        readback = (await self.post(mode="readback")).json()["result"]
        self.assertTrue(readback["configuration_matches"])
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
        self.assertEqual(retained.status_code, 409, retained.text)
        self.assertEqual(
            retained.json()["error"]["code"], "application_state_volume_already_adopted"
        )
        calls = self.provider.call_count
        recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertTrue(recovered.json()["result"]["applied"])
        self.assertEqual(self.provider.call_count, calls)

    async def test_committed_adoption_receipt_recovers_without_provider_evidence(self) -> None:
        await self.plan()
        with patch(
            "control_plane.application_state_volume.ApplicationStateVolumeAdapter._verify_adopted",
            side_effect=OSError("Fixture: interrupted after adoption"),
        ):
            uncertain = await self.post()
        self.assertEqual(uncertain.status_code, 409, uncertain.text)
        calls = self.provider.call_count
        # Receipt completion uses committed authority, even after provider host
        # rotation, mount edits, or an inventory outage. It proves no live mount.
        with (
            patch(
                "control_plane.dokploy_target_setup_http.dokploy_source.read_dokploy_config",
                return_value=("https://rotated.example.invalid", "fixture-token"),
            ),
            patch(
                "control_plane.dokploy_inventory.read_dokploy_inventory",
                side_effect=AssertionError("Receipt must not require provider inventory"),
            ),
            patch(
                "control_plane.dokploy_target_setup_http.fetch_dokploy_target_payload_for_setup",
                side_effect=AssertionError("Receipt must not require provider readback"),
            ),
        ):
            recovered = await self.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        result = recovered.json()["result"]
        self.assertTrue(result["applied"])
        self.assertEqual(result["allocation_state"], "adopted")
        self.assertEqual(result["configuration_evidence"], "unavailable")
        self.assertFalse(result["configuration_matches"])
        self.assertEqual(self.provider.call_count, calls)
        self.assertFalse(self.store.list_held_provider_target_reservations())
        self.assert_production_unchanged()

    async def test_pre_effect_busy_refusal_preserves_code_and_does_not_hold_the_lane(self) -> None:
        from contextlib import nullcontext

        await self.plan()
        with patch.object(
            self.store, "testing_replacement_execution_lock", return_value=nullcontext(None)
        ):
            busy = await self.post()
        self.assertEqual(busy.status_code, 409, busy.text)
        self.assertEqual(busy.json()["error"]["code"], "application_state_volume_lane_busy")
        self.provider.assert_not_called()
        self.assertFalse(self.store.list_held_provider_target_reservations())
        retry = await self.post()
        self.assertEqual(retry.status_code, 202, retry.text)

    async def test_pre_effect_owner_refusal_preserves_code(self) -> None:
        from control_plane.application_state_volume import ApplicationStateVolumeAdapter

        await self.plan()
        checkpoint = ApplicationStateVolumeAdapter.checkpoint
        retiring = self.profile.model_copy(
            update={
                "lifecycle_state": "retiring",
                "preview": self.profile.preview.model_copy(update={"enabled": False}),
            }
        )

        def retire_before_checkpoint(adapter: ApplicationStateVolumeAdapter) -> None:
            self.store.write_product_profile_record(retiring)
            checkpoint(adapter)

        with patch.object(
            ApplicationStateVolumeAdapter, "checkpoint", new=retire_before_checkpoint
        ):
            result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(result.json()["error"]["code"], "application_state_volume_owner_changed")
        self.provider.assert_not_called()
        self.assertFalse(self.store.list_held_provider_target_reservations())

    async def test_completed_adoption_retention_returns_its_caller_action(self) -> None:
        await self.plan()
        success = await self.post()
        self.assertEqual(success.status_code, 202, success.text)
        readback = (await self.post(mode="readback")).json()["result"]
        calls = self.provider.call_count
        retained = await self.post(
            retain_state_volume_allocation=True,
            expected_state_volume_custody_sha256=readback["custody_sha256"],
        )
        self.assertEqual(retained.status_code, 409, retained.text)
        self.assertEqual(
            retained.json()["error"]["code"], "application_state_volume_already_adopted"
        )
        self.assertFalse(self.store.list_held_provider_target_reservations())
        self.assertEqual(self.provider.call_count, calls)

    async def test_removed_verified_mount_never_repeats_creation_and_can_retain(self) -> None:
        from datetime import timedelta
        from control_plane.application_state_volume import ApplicationStateVolumeAdapter
        from control_plane.contracts.idempotency_record import (
            format_launchplane_mutation_timestamp,
            parse_launchplane_mutation_timestamp,
        )

        await self.plan()
        checkpoint = ApplicationStateVolumeAdapter._checkpoint_phase

        def interrupt_after_mount(
            adapter: ApplicationStateVolumeAdapter, lease: Any, phase: str
        ) -> None:
            checkpoint(adapter, lease, phase)
            if json.loads(phase).get("mount_verified") == "true":
                raise OSError("Fixture: stopped after verified mount, before adoption")

        with patch.object(
            ApplicationStateVolumeAdapter, "_checkpoint_phase", new=interrupt_after_mount
        ):
            uncertain = await self.post()
        self.assertEqual(uncertain.status_code, 409, uncertain.text)
        calls = self.provider.call_count
        self.live["mounts"] = []
        retry = await self.post()
        self.assertEqual(retry.status_code, 409, retry.text)
        self.assertEqual(self.provider.call_count, calls)
        with self.assertRaises(FileNotFoundError):
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            )
        row = self.store.list_held_provider_target_reservations()[0]
        readback = (await self.post(mode="readback")).json()["result"]
        self.assertEqual(readback["configuration_evidence"], "differs")
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
        self.assertFalse(retained.json()["result"]["applied"])
        self.assertEqual(self.provider.call_count, calls)
        fresh = await self.post(mode="dry-run", state_volume_plan_id="", expected_plan_sha256="")
        self.assertEqual(fresh.status_code, 202, fresh.text)
        self.assert_production_unchanged()

    async def test_recovered_pending_mount_removed_before_replay_is_not_created_again(self) -> None:
        from control_plane.application_state_volume import ApplicationStateVolumeAdapter

        await self.plan()
        self.fail_after = MOUNT_CREATE
        uncertain = await self.post()
        self.assertEqual(uncertain.status_code, 409, uncertain.text)
        self.fail_after = ""
        calls = self.provider.call_count
        checkpoint = ApplicationStateVolumeAdapter._checkpoint_phase

        def remove_after_recovery(
            adapter: ApplicationStateVolumeAdapter, lease: Any, phase: str
        ) -> None:
            checkpoint(adapter, lease, phase)
            progress = json.loads(phase)
            if progress.get("application") and not progress.get("pending"):
                self.live["mounts"] = []

        with patch.object(
            ApplicationStateVolumeAdapter, "_checkpoint_phase", new=remove_after_recovery
        ):
            retry = await self.post()
        self.assertEqual(retry.status_code, 409, retry.text)
        self.assertEqual(self.provider.call_count, calls)
        with self.assertRaises(FileNotFoundError):
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            )
        repeated = await self.post()
        self.assertEqual(repeated.status_code, 409, repeated.text)
        self.assertEqual(self.provider.call_count, calls)
        self.assert_production_unchanged()

    async def test_authority_change_at_final_commit_refuses_and_preserves_custody(self) -> None:
        from datetime import timedelta
        from control_plane.contracts.idempotency_record import (
            format_launchplane_mutation_timestamp,
            parse_launchplane_mutation_timestamp,
        )

        await self.plan()
        write = self.store.write_product_authority_bundle
        changed = self.profile.model_copy(update={"driver_id": "odoo"})

        def change_before_commit(bundle: Any) -> None:
            self.store.write_product_profile_record(changed)
            write(bundle)

        with patch.object(
            self.store, "write_product_authority_bundle", side_effect=change_before_commit
        ):
            result = await self.post()
        self.assertEqual(result.status_code, 409, result.text)
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), changed)
        with self.assertRaises(FileNotFoundError):
            self.store.read_dokploy_target_id_record(
                context_name=self.context, instance_name="testing"
            )
        row = self.store.list_held_provider_target_reservations()[0]
        readback = (await self.post(mode="readback")).json()["result"]
        self.assertEqual(
            json.loads(readback["provider_effect_phase"])["application"], self.live["applicationId"]
        )
        now = format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(
                row.lease_expires_at, field_name="lease_expires_at"
            )
            + timedelta(seconds=1)
        )
        calls = self.provider.call_count
        with patch.object(self.store, "_database_mutation_timestamp", return_value=now):
            retained = await self.post(
                retain_state_volume_allocation=True,
                expected_state_volume_custody_sha256=readback["custody_sha256"],
            )
        self.assertEqual(retained.status_code, 202, retained.text)
        self.assertFalse(retained.json()["result"]["applied"])
        self.assertEqual(self.provider.call_count, calls)
        self.store.write_product_profile_record(self.profile)
        fresh = await self.post(mode="dry-run", state_volume_plan_id="", expected_plan_sha256="")
        self.assertEqual(fresh.status_code, 202, fresh.text)
        self.assertFalse(self.store.list_held_provider_target_reservations())
        self.assert_production_unchanged()

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
