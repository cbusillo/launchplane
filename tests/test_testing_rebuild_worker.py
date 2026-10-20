"""The durable worker joins preparation, unchanged routing, adoption and retirement."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from urllib.parse import urlsplit
from typing import Any, Literal
import unittest
from unittest.mock import Mock, patch

from control_plane import testing_rebuild_service as service
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementApplyResult,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.testing_rebuild import (
    REBUILD_SCOPE,
    REBUILD_ROUTE,
    TestingCopy,
    TestingRebuildRecord,
)
from control_plane.contracts.testing_candidate import (
    TestingCandidateAllocation,
    testing_candidate_namespace,
)
from control_plane.contracts.testing_candidate_application import TestingCandidatePrepared
from control_plane.dokploy.testing_route import route_document, compile_testing_route
from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport
from control_plane.dokploy import post_deploy, api, source
from control_plane import testing_candidate_application as candidate_application
from control_plane.testing_candidate_allocation import candidate_resources
from control_plane.testing_candidate_runtime import candidate_runtime_inputs
from control_plane.testing_local_credentials import generation_bundle
from control_plane.testing_serving_copy import (
    published_testing_copy,
    render_maintained_testing_copy,
)
from control_plane.odoo_testing_usage import OdooTestingUsage
from control_plane.testing_publication_storage import read_snapshot, publish as publication_write
from tests import test_testing_publication as publication_tests
from tests import test_testing_route as route_tests
from tests.test_testing_rebuild_service import exercise_published_copy_maintenance


class NativeTestingWorkerTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        self.fixture = publication_tests.TestingPublicationTests()
        self.fixture.database_url = self.database_url
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.root = self.fixture.store, self.fixture.fixture.root
        # The storage fixture begins with a stranded legacy queue item. These
        # worker cases model an admitted rebuild after the previous item ended.
        for previous in self.store.list_odoo_stable_target_replacement_operation_records():
            if previous.status == "reconciliation_required":
                finished = datetime.now(UTC).isoformat()
                self.store.write_odoo_stable_target_replacement_operation_record(
                    previous.model_copy(
                        update={
                            "status": "fail",
                            "phase": "failed",
                            "updated_at": finished,
                            "finished_at": finished,
                        }
                    )
                )
        candidate = self.fixture.candidate
        assert candidate.application is not None and candidate.application.prepared is not None
        self.app = candidate.application
        self.prepared = candidate.application.prepared
        self.original_identity = RuntimeIdentity(
            product=candidate.product,
            context=candidate.context,
            instance="testing",
            deployment_record_id="old-deployment",
            artifact_id="old-build",
            source_git_ref="old-source",
        )
        self.operation = OdooStableTargetReplacementOperationRecord(
            operation_id=candidate.operation_id,
            product=candidate.product,
            context=candidate.context,
            instance="testing",
            idempotency_key="native-test",
            idempotency_scope="fixture",
            request_fingerprint="native-worker-fixture",
            request=OdooStableTargetReplacementApplyRequest(
                product=candidate.product,
                instance="testing",
                strategy="replace-and-cutover",
                data_source_mode="empty",
                allow_empty_data=True,
                artifact_id=self.fixture.fixture.artifact.artifact_id,
                source_git_ref=self.fixture.fixture.artifact.source_commit,
            ),
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
        self.retained: dict[str, Any] = {
            "web_id": "old-web-id",
            "identity": self.original_identity.model_dump(mode="json"),
            "resource_binding": "retained-data",
            "physical_binding": "retained-physical-data",
            "stopped": False,
            "web_stopped": False,
            "type": "complete",
        }
        self.intent = service.NativeTestingRebuildIntent(
            operation_id=candidate.operation_id,
            profile=self.store.read_product_profile_record(candidate.product),
            before=self.fixture.before,
            server_id=candidate.server_id,
            project="old-testing",
            old=TestingCopy(
                copy_id=candidate.serving_target_id,
                target_id=candidate.serving_target_id,
                private_url=self.fixture.route.old_backend,
                resource_binding=self.retained["physical_binding"],
                runtime_identity=self.original_identity,
            ),
            retained=self.retained,
            public_entrypoints=self.fixture.route.public_entrypoints,
            old_auto_deploy=False,
        )
        routed = route_tests.TestingRouteTests()
        routed.setUp()
        self.raw = deepcopy(routed.raw)
        for router in self.raw["routers"].values():
            router["rule"] = f"Host(`{self.fixture.route.host}`)"
        self.owned: dict[str, Any] | None = None
        self.effects: list[str] = []
        self.fail_public = False
        self.fail_after_stop = False
        self.old_private_healthy = True
        self.authorize = Mock()
        self.old_provider: dict[str, Any] = {
            "composeId": self.intent.old.target_id,
            "name": self.fixture.before.target.target_name,
            "appName": self.intent.project,
            "serverId": self.intent.server_id,
            "autoDeploy": False,
            "composeType": "docker-compose",
        }
        self.enterContext(
            patch.object(
                api,
                "fetch_dokploy_target_payload",
                side_effect=lambda **kwargs: (
                    dict(self.old_provider)
                    if kwargs["target_id"] == self.intent.old.target_id
                    else dict(self.fixture.fixture.provider_state)
                ),
            )
        )

        self.capture_function = service.capture_rebuild_intent
        self.capture = self.enterContext(
            patch.object(service, "capture_rebuild_intent", return_value=self.intent)
        )
        self.enterContext(
            patch.object(service, "retained_copy_operation", side_effect=self.physical)
        )
        self.enterContext(patch.object(api, "dokploy_request", side_effect=self.provider_request))
        self.deployment_history = self.enterContext(
            patch.object(api, "list_deployments_for_target", return_value=[])
        )
        self.enterContext(
            patch.object(
                service,
                "read_odoo_testing_usage",
                side_effect=lambda **_: (
                    OdooTestingUsage("unknown")
                    if self.retained["stopped"]
                    else OdooTestingUsage("unused", False, False, "complete")
                ),
            )
        )
        self.enterContext(
            patch.object(post_deploy, "compose_data_workflow_is_quiescent", return_value=True)
        )
        self.real_route_transport = {
            name: getattr(DokployTestingRouteTransport, name)
            for name in ("read", "publish", "settle_attempt")
        }
        self.enterContext(
            patch.object(DokployTestingRouteTransport, "read", side_effect=self.route_read)
        )
        self.enterContext(
            patch.object(DokployTestingRouteTransport, "publish", side_effect=self.switch)
        )
        self.enterContext(
            patch.object(
                DokployTestingRouteTransport,
                "settle_attempt",
                side_effect=lambda attempt, **kwargs: self.settle_execution(
                    attempt.execution, attempt.key, **kwargs
                ),
            )
        )
        from control_plane.dokploy.testing_route_execution import TestingRouteFence
        from control_plane.testing_route_effects import TestingRouteFenceController

        self.real_route_fence_ensure = TestingRouteFenceController.ensure

        self.route_fence_ensure = self.enterContext(
            patch.object(
                TestingRouteFenceController,
                "ensure",
                return_value=TestingRouteFence(
                    generation="a" * 64,
                    lock_identity="1:2",
                    revision=0,
                    key="none",
                    desired="absent",
                    original="absent",
                    state="initialized",
                ),
            )
        )
        self.enterContext(
            patch.object(
                service,
                "candidate_private_readiness",
                side_effect=lambda **_: self.prepared.model_copy(
                    update={"observed_at": datetime.now(UTC)}
                ),
            )
        )
        self.enterContext(
            patch.object(service, "read_public_testing_health", side_effect=self.health)
        )

    def current(self) -> Any:
        return read_snapshot(
            self.store, product=self.operation.product, context=self.operation.context
        )

    def provider_request(self, **kwargs: Any) -> dict[str, Any]:
        self.assertEqual(kwargs["path"], "/api/compose.stop")
        self.assertEqual(kwargs["payload"]["composeId"], self.intent.old.target_id)
        self.assertIsNotNone(self.owned)
        self.assertEqual(self.current().target_id.target_id, self.fixture.candidate.compose_id)

        self.assertFalse(self.fail_public)
        self.effects.append("stop-old")
        self.retained["stopped"] = self.retained["web_stopped"] = True
        # The Docker provider removes stopped containers' routers. Retaining
        # their synthetic enabled routers would describe a different host.
        self.raw["routers"] = {
            name: router
            for name, router in self.raw["routers"].items()
            if not name.endswith("@docker")
        }
        if self.fail_after_stop:
            self.fail_after_stop = False
            raise TimeoutError("fixture lost retirement reply")
        return {}

    def physical(self, **kwargs: Any) -> dict[str, Any]:
        result = dict(self.retained)
        if kwargs["payload"]["mode"] == "readiness":
            result["healthy"] = self.old_private_healthy
        return result

    def route_read(self) -> dict[str, Any]:
        endpoints = [] if self.retained["stopped"] else [self.intent.old.private_url]
        legacy = self.raw["services"].get("old@docker")
        if endpoints and legacy is not None:
            endpoints.extend(item["url"] for item in legacy["loadBalancer"]["servers"])
        return {
            "raw": deepcopy(self.raw),
            "owned_file": json.dumps(self.owned) if self.owned is not None else None,
            "old_endpoints": tuple(endpoints),
            "file_sha256": canonical_json_sha256(self.owned)
            if self.owned is not None
            else "absent",
            "execution_custody": {
                "mount_source": "/fixture/dynamic",
                "filesystem": "1:1",
                "proxy_id": "fixture-proxy",
                "proxy_started_at": "fixture-original-start",
                "process_epoch": "fixture-original-process",
            },
        }

    def settle_execution(self, execution: Any, key: str, *, dispatch_guard: Any) -> str:
        dispatch_guard(lambda: None)
        if self.owned is not None:
            backend = self.owned["http"]["services"][self.fixture.route.route_id]["loadBalancer"][
                "servers"
            ][0]["url"]
            if backend == execution.backend:
                return "applied"
        return "unknown"

    def switch(
        self,
        plan: Any,
        backend: str,
        *,
        expected_backend: str,
        dispatch_guard: Any,
        desired_runtime_identity: RuntimeIdentity | None = None,
        execution: Any = None,
        execution_key: str = "",
        writer_attempt: Any = None,
    ) -> str:
        def publish() -> None:
            if backend == plan.new_backend:
                self.assertEqual(self.current().target_id, self.fixture.before.target_id)
                self.assertEqual(self.current().provider, self.fixture.before.provider)
            self.assertFalse(self.retained["stopped"])
            self.effects.append("switch-new" if backend == plan.new_backend else "switch-old")
            self.owned = route_document(plan, backend)
            for section in ("routers", "services", "middlewares"):
                for name, value in self.owned["http"].get(section, {}).items():
                    self.raw[section][name + "@file"] = {**value, "status": "enabled"}

        dispatch_guard(publish)
        return "applied"

    def health(self, base_url: str, path: str) -> HealthcheckEvidence:
        identity = (
            self.original_identity
            if self.owned is None
            or self.owned["http"]["services"][self.fixture.route.route_id]["loadBalancer"][
                "servers"
            ][0]["url"]
            == self.fixture.route.old_backend
            else self.app.plan.runtime_identity
        )
        return self.fixture.health.model_copy(
            update={
                "observed_runtime_identity": identity,
                "status": "fail"
                if self.fail_public and identity != self.original_identity
                else "pass",
            }
        )

    def execute(self) -> Any:
        return service.execute_native_testing_rebuild(
            root=self.root,
            store=self.store,
            operation=self.operation,
            authorize=self.authorize,
            lease_owner="native-worker-fixture",
        )

    def test_capacity_refusal_releases_fresh_reservation_and_preserves_serving_copy(self) -> None:
        from control_plane.dokploy import native_copy_stream
        from control_plane.dokploy.network_capacity import (
            BRIDGE_NETWORK_CAPACITY_MARGIN,
            BridgeNetworkCapacityRefused,
        )
        from control_plane import testing_candidate_allocation as allocation_service

        self.operation = self.operation.model_copy(
            update={"operation_id": self.operation.operation_id + "-capacity"}
        )
        self.intent = self.intent.model_copy(update={"operation_id": self.operation.operation_id})
        self.capture.return_value = self.intent
        namespace = testing_candidate_namespace(
            self.operation.product, self.operation.context, self.operation.operation_id
        )
        denial = native_copy_stream.NativeSourceAuthorizationDenied("fixture capacity denial")
        for observation in (0, BRIDGE_NETWORK_CAPACITY_MARGIN, None, denial):
            with self.subTest(observation=observation):

                def capacity(**kwargs: Any) -> None:
                    self.assertEqual(kwargs["server_id"], self.intent.server_id)
                    if observation is denial:
                        raise denial
                    if observation is not None:
                        kwargs["receive"](
                            {
                                "type": "complete",
                                "result": {"total_slots": 16, "free_slots": observation},
                            }
                        )

                with (
                    patch.object(
                        allocation_service, "created_placement", return_value=("", "", "")
                    ),
                    patch.object(native_copy_stream, "_read_remote", side_effect=capacity),
                    patch.object(allocation_service, "create_isolated_placement") as create,
                    patch.object(allocation_service, "run_resource_schedule") as storage,
                    self.assertRaises(
                        native_copy_stream.NativeSourceAuthorizationDenied
                        if observation is denial
                        else BridgeNetworkCapacityRefused
                    ),
                ):
                    self.execute()
                create.assert_not_called()
                storage.assert_not_called()
                self.assertIsNone(self.store.read_testing_candidate(namespace))
                self.assertFalse(service.native_rebuild_effects_started(self.store, self.operation))
                self.assertEqual(
                    self.store.lookup_existing_mutation_reservation(
                        route_path=REBUILD_ROUTE,
                        idempotency_key=self.operation.operation_id,
                        request_fingerprint=self.operation.request_fingerprint,
                    ).status,
                    "missing",
                )
                self.assertEqual(self.current(), self.fixture.before)
                self.assertEqual(self.effects, [])

    def assert_failed_checkpoint(self, result: Any) -> None:
        from control_plane.testing_rebuild_storage import RecordTestingRebuildStore

        checkpoint = RecordTestingRebuildStore(self.store).load_operation(
            product=self.operation.product,
            context=self.operation.context,
            operation_id=self.operation.operation_id,
        )
        self.assertTrue(checkpoint.error)
        self.assertEqual(result.error_code, checkpoint.error)
        self.assertFalse(result.runtime_identity_injected)
        self.assertEqual(result.target_id, self.intent.old.target_id)

    def cancellation(self) -> OdooStableTargetReplacementOperationRecord:
        from control_plane.contracts.durable_operation_authorization import (
            DurableOperationCancellation,
            DurableOperationCallerIdentity,
        )

        at = datetime.now(UTC).isoformat()
        return self.operation.model_copy(
            update={
                "status": "cancelled",
                "phase": "cancelled",
                "updated_at": at,
                "finished_at": at,
                "cancellation": DurableOperationCancellation(
                    reason="Cancel the original testing request",
                    cancelled_at=at,
                    caller=DurableOperationCallerIdentity(
                        identity_type="local_operator", subject="fixture", token_label="fixture"
                    ),
                ),
            }
        )

    def test_native_cancellation_before_execution_has_no_provider_effect(self) -> None:
        from control_plane.storage.postgres import LaunchplaneTestingCandidateRow

        # This fixture otherwise starts with a real, fully prepared private copy.
        # An unstarted allocation can exist, but has no provider effect or ids.
        with self.store._session_factory.begin() as session:
            row = session.get(LaunchplaneTestingCandidateRow, self.fixture.candidate.namespace)
            assert row is not None
            row.payload = self.fixture.candidate.model_copy(
                update={
                    "effect": "",
                    "project_id": "",
                    "environment_id": "",
                    "compose_id": "",
                    "resources_binding_sha256": "",
                    "application": None,
                }
            ).model_dump(mode="json")
        self.store.write_odoo_stable_target_replacement_operation_record(self.operation)
        self.assertTrue(
            self.store.cancel_pending_odoo_stable_target_replacement_operation_record(
                self.cancellation()
            )
        )
        self.assertEqual(self.effects, [])

    def test_private_candidate_without_a_current_outer_marker_cannot_be_cancelled(self) -> None:
        self.store.write_odoo_stable_target_replacement_operation_record(self.operation)
        self.assertFalse(
            self.store.cancel_pending_odoo_stable_target_replacement_operation_record(
                self.cancellation()
            )
        )
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.operation.operation_id
            ),
            self.operation,
        )

    def test_pending_native_effect_cannot_be_cancelled_and_original_observation_completes(
        self,
    ) -> None:
        from control_plane.contracts.promotion_record import HealthcheckEvidence

        self.store.write_odoo_stable_target_replacement_operation_record(self.operation)

        def health(base_url: str, path: str) -> HealthcheckEvidence:
            if self.owned is not None:
                return HealthcheckEvidence(status="pending", urls=(base_url,))
            return self.health(base_url, path)

        with patch.object(service, "read_public_testing_health", side_effect=health):
            with self.assertRaises(service.NativeTestingRebuildPending):
                self.execute()
        self.assertEqual(self.effects, ["switch-new"])
        self.assertFalse(
            self.store.cancel_pending_odoo_stable_target_replacement_operation_record(
                self.cancellation()
            )
        )
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(
                self.operation.operation_id
            ),
            self.operation,
        )
        import asyncio
        from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
        from control_plane.http_app import create_launchplane_fastapi_app
        from tests.http_app_test_support import (
            _odoo_operation_status_identity,
            _odoo_operation_status_policy,
        )
        from tests.support.auth import _StubVerifier
        from tests.support.http import request

        policy = _odoo_operation_status_policy(
            action="odoo_target_replacement_apply.execute",
            products=(self.operation.product,),
            contexts=(self.operation.context,),
            instances=("testing",),
            schema_version=2,
        )
        self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id="native-cancellation-fixture-policy",
                source="fixture",
                updated_at=self.operation.updated_at,
                revision=1,
                policy=policy,
            )
        )
        app = create_launchplane_fastapi_app(
            control_plane_root_path=self.root,
            verifier=_StubVerifier(_odoo_operation_status_identity()),
            authz_policy=policy,
            record_store_factory=lambda: self.store,
        )
        response = asyncio.run(
            request(
                app,
                "POST",
                f"/v1/drivers/odoo/target-replacement/operations/{self.operation.operation_id}/cancel",
                headers={"Authorization": "Bearer valid-token"},
                payload={"reason": "Cancel this pending rebuild"},
            )
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "testing_rebuild_observation_required")
        self.assertTrue(response.json()["trace_id"])
        self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def test_initial_capture_maps_the_observed_ip_to_one_of_its_physical_dns_names(self) -> None:
        frame = self.route_read()
        aliases = (self.intent.old.private_url, "http://old-testing-web-1:8069")
        frame["old_endpoints"] = (*aliases, *frame["old_endpoints"])
        with patch.object(DokployTestingRouteTransport, "read", return_value=frame):
            captured = self.capture_function(
                root=self.root, store=self.store, operation=self.operation
            )
        self.assertIn(captured.old.private_url, aliases)
        self.assertEqual(captured.old.runtime_identity, self.original_identity)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current(), self.fixture.before)

    def test_capture_derives_only_application_ingress_from_the_complete_proxy_frame(self) -> None:
        frame = self.route_read()
        route_tests.add_proxy_internal_routes(frame["raw"])
        frame["raw"]["services"]["alias@docker"] = deepcopy(frame["raw"]["services"]["old@docker"])
        for name, router in list(frame["raw"]["routers"].items()):
            if name.endswith("@docker"):
                frame["raw"]["routers"]["alias-" + name] = {
                    **deepcopy(router),
                    "service": "alias@docker",
                }
        with patch.object(DokployTestingRouteTransport, "read", return_value=frame):
            captured = self.capture_function(
                root=self.root, store=self.store, operation=self.operation
            )
        self.assertEqual(captured.public_entrypoints, ("web", "websecure"))
        self.assertEqual(captured.old.runtime_identity, self.original_identity)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current(), self.fixture.before)
        host = self.fixture.route.host
        for provider, rule in (
            ("docker", f"Host(`{host}`) && PathPrefix(`/x`)"),
            ("docker", f"Host(`{host.upper()}`)"),
            ("docker", f"Host(`{host}`) || Host(`other.invalid`)"),
            ("swarm", f"Host(`{host}`)"),
            ("futureprovider", f"Host(`{host}`)"),
        ):
            conflicting_frame = deepcopy(frame)
            conflicting_frame["raw"]["routers"][f"conflict@{provider}"] = {
                **deepcopy(frame["raw"]["routers"]["old-http@docker"]),
                "rule": rule,
                "entryPoints": ["another-listener"],
            }
            with (
                self.subTest(provider=provider, rule=rule),
                patch.object(DokployTestingRouteTransport, "read", return_value=conflicting_frame),
            ):
                with self.assertRaises(service.TestingRebuildRefused) as refused:
                    self.capture_function(
                        root=self.root, store=self.store, operation=self.operation
                    )
                self.assertEqual(refused.exception.code, "testing_route_compilation_failed")
                self.assertEqual(self.effects, [])
                self.assertEqual(self.current(), self.fixture.before)

    def test_transient_initial_route_capture_keeps_the_admitted_worker_pending(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        self.operation = self.operation.model_copy(
            update={"operation_id": "fresh-transient-capture"}
        )
        self.capture.side_effect = self.capture_function
        with patch.object(
            DokployTestingRouteTransport,
            "read",
            side_effect=NativeSourceUnavailable("fixture unavailable"),
        ):
            with self.assertRaises(service.NativeTestingRebuildPending):
                self.execute()
        self.capture.assert_called_once()
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current(), self.fixture.before)

    def test_capture_reports_route_checkpoints_without_provider_details_or_writes(self) -> None:
        from control_plane.dokploy.native_copy_stream import (
            NativeSourceUnavailable,
            NativeSourceAuthorizationDenied,
        )

        for failure in (
            NativeSourceUnavailable("private-host-message"),
            NativeSourceAuthorizationDenied("private-denial-message"),
        ):
            with patch.object(DokployTestingRouteTransport, "read", side_effect=failure):
                if isinstance(failure, NativeSourceAuthorizationDenied):
                    with self.assertRaises(NativeSourceAuthorizationDenied):
                        self.capture_function(
                            root=self.root, store=self.store, operation=self.operation
                        )
                else:
                    with self.assertRaises(NativeSourceUnavailable):
                        self.capture_function(
                            root=self.root, store=self.store, operation=self.operation
                        )
        from control_plane.dokploy.testing_route_transport import TestingRouteObservationRefused

        for observation_failure in (
            TestingRouteObservationRefused(
                "testing_route_identity_changed", "Retained identity changed."
            ),
            TestingRouteObservationRefused(
                "testing_route_provider_unverified", "Watched provider unverified."
            ),
        ):
            with patch.object(
                DokployTestingRouteTransport, "read", side_effect=observation_failure
            ):
                with self.assertRaises(service.TestingRebuildRefused) as refused:
                    self.capture_function(
                        root=self.root, store=self.store, operation=self.operation
                    )
                self.assertEqual(refused.exception.code, observation_failure.code)
        for owned in ("private-invalid-json", "[]", "null"):
            frame = {**self.route_read(), "owned_file": owned}
            with patch.object(DokployTestingRouteTransport, "read", return_value=frame):
                with self.assertRaises(service.TestingRebuildRefused) as refused:
                    self.capture_function(
                        root=self.root, store=self.store, operation=self.operation
                    )
                self.assertEqual(refused.exception.code, "testing_route_file_invalid")
                self.assertNotIn("private-", str(refused.exception))
        frame = self.route_read()
        frame["raw"]["routers"]["conflict@docker"] = {
            **deepcopy(frame["raw"]["routers"]["old-http@docker"]),
            "priority": 900,
        }
        with patch.object(DokployTestingRouteTransport, "read", return_value=frame):
            with self.assertRaises(service.TestingRebuildRefused) as refused:
                self.capture_function(root=self.root, store=self.store, operation=self.operation)
            self.assertEqual(refused.exception.code, "testing_route_compilation_failed")
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current(), self.fixture.before)

    def test_capture_refuses_a_removed_lane_with_retained_context_ownership(self) -> None:
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": tuple(
                        lane for lane in profile.lanes if lane.context != self.operation.context
                    ),
                    "historical_contexts": (*profile.historical_contexts, self.operation.context),
                }
            )
        )
        with patch.object(DokployTestingRouteTransport, "read") as read:
            with self.assertRaises(service.TestingRebuildRefused) as refused:
                self.capture_function(root=self.root, store=self.store, operation=self.operation)
        self.assertEqual(refused.exception.code, "testing_rebuild_inputs_unverified")
        read.assert_not_called()
        self.assertEqual(self.effects, [])

    def test_one_action_preserves_address_credentials_and_retires_only_after_publication(
        self,
    ) -> None:
        secrets = self.store.list_secret_records()
        result = self.execute()
        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])
        self.assertEqual(
            tuple(item.domain_name for item in self.current().binding.domains),
            (urlsplit(self.fixture.fixture.compiled.plan.base_url).hostname,),
        )
        self.assertEqual(self.store.list_secret_records(), secrets)
        self.assertEqual(self.execute(), result)
        self.capture.assert_called_once()
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def test_failed_public_candidate_restores_the_old_route_without_retiring_it(self) -> None:
        self.fail_public = True
        result = self.execute()
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(self.effects, ["switch-new", "switch-old"])
        self.assertFalse(self.retained["stopped"])
        self.assertEqual(self.current(), self.fixture.before)

    def apply_staff_hold(self, held: bool, reason: str) -> Any:
        from control_plane.testing_lane_hold import (
            TestingHoldApplyRequest,
            apply_testing_hold_plan,
            build_testing_hold_plan,
        )

        request = TestingHoldApplyRequest(
            product=self.operation.product,
            context=self.operation.context,
            instance="testing",
            hold=held,
            reason=reason,
        )
        plan, _ = build_testing_hold_plan(
            record_store=self.store, request=request, actor="fixture-admin"
        )
        apply_testing_hold_plan(
            record_store=self.store,
            request=request.model_copy(
                update={"mode": "apply", "reviewed_plan_sha256": plan.plan_sha256}
            ),
            actor="fixture-admin",
            required_context_owner=(self.operation.product, self.operation.context),
        )
        return self.current().target.policies.staff_testing_hold

    def test_real_hold_set_and_clear_during_preparation_allows_completion(self) -> None:
        original = service.NativeTestingRebuildExecution.prepare

        def changed(executor: Any, plan: Any) -> Any:
            receipt = original(executor, plan)
            self.apply_staff_hold(True, "Fixture staff test during preparation")
            self.apply_staff_hold(False, "Fixture staff test complete")
            return receipt

        with patch.object(service.NativeTestingRebuildExecution, "prepare", changed):
            self.assertEqual(self.execute().deploy_status, "pass")
        self.assertIsNone(self.current().target.policies.staff_testing_hold)
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def test_real_hold_set_and_clear_before_input_resume_preserves_candidate(self) -> None:
        original = self.store.read_testing_candidate(self.fixture.candidate.namespace)
        assert original is not None
        self.apply_staff_hold(True, "Staff testing while the candidate is prepared")
        with self.assertRaises(service.NativeTestingRebuildPending):
            self.execute()
        self.assertEqual(self.effects, [])
        self.assertEqual(self.store.read_testing_candidate(original.namespace), original)
        self.apply_staff_hold(False, "Staff testing finished")
        self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def test_real_hold_before_application_plan_preserves_original_input_authority(self) -> None:
        from control_plane.storage.postgres import LaunchplaneTestingCandidateRow

        candidate = self.fixture.candidate.model_copy(update={"application": None})
        with self.store._session_factory.begin() as session:
            row = session.get(LaunchplaneTestingCandidateRow, candidate.namespace)
            assert row is not None
            row.payload = candidate.model_dump(mode="json", exclude_none=True)
        self.apply_staff_hold(True, "Staff testing during independent allocation")
        self.apply_staff_hold(False, "Staff testing finished before application planning")

        def prepared(**kwargs: Any) -> Any:
            # Exercise the real input compiler and publication validator around
            # an already-proven private application fixture.
            inputs = kwargs["inputs"]
            proof = self.prepared.model_copy(
                update={
                    "plan_sha256": inputs.plan.digest,
                    "source_witness": "empty:" + inputs.plan.digest,
                }
            )
            self.app = self.app.model_copy(update={"plan": inputs.plan, "prepared": proof})
            self.prepared = proof
            updated = candidate.model_copy(update={"application": self.app})
            self.store.compare_and_write_testing_candidate(candidate, updated)
            return updated

        with (
            patch.object(service, "prepare_candidate_storage", return_value=candidate),
            patch.object(service, "prepare_candidate_application", side_effect=prepared),
        ):
            self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def test_hold_after_cutover_does_not_block_inverse_or_erase_the_hold(self) -> None:
        self.fail_public = True
        hold: Any = None

        def held(plan: Any, backend: str, **kwargs: Any) -> str:
            nonlocal hold
            self.switch(plan, backend, **kwargs)
            if backend == plan.new_backend:
                hold = self.apply_staff_hold(True, "Staff need the testing lane")
            return "applied"

        with (
            patch.object(DokployTestingRouteTransport, "publish", side_effect=held),
        ):
            self.assertEqual(self.execute().deploy_status, "fail")
        self.assertEqual(self.effects, ["switch-new", "switch-old"])
        self.assertIsNotNone(hold)
        self.assertEqual(self.current().target.policies.staff_testing_hold, hold)
        self.assertFalse(self.retained["stopped"])
        self.assertEqual(self.current().target_id, self.fixture.before.target_id)

    def change_unrelated_profile(self) -> None:
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "display_name": profile.display_name + " updated",
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
        )

    def test_unrelated_profile_edit_after_admission_does_not_strand_the_rebuild(self) -> None:
        self.change_unrelated_profile()
        self.assertEqual(self.execute().deploy_status, "pass")
        self.assertTrue(
            self.store.read_product_profile_record(self.operation.product).display_name.endswith(
                " updated"
            )
        )

    def test_unrelated_profile_edit_after_cutover_does_not_strand_publication(self) -> None:
        def edited(plan: Any, backend: str, **kwargs: Any) -> str:
            self.switch(plan, backend, **kwargs)
            if backend == plan.new_backend:
                self.change_unrelated_profile()
            return "applied"

        with patch.object(DokployTestingRouteTransport, "publish", side_effect=edited):
            self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def test_changed_runtime_before_effects_refuses_without_fencing_the_lane(self) -> None:
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "runtime_port": profile.runtime_port + 1,
                }
            )
        )
        result = self.execute()
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.error_code, "testing_rebuild_admission_changed")
        original = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE,
            idempotency_key=self.operation.operation_id,
        )
        self.assertIsNotNone(original)
        assert original is not None
        self.assertFalse(original.provider_effect_started_at)
        self.assertEqual(self.effects, [])

    def hold_after_private_candidate_effects(self, *, prepared: bool = False) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
        from control_plane.testing_rebuild_storage import RecordTestingRebuildStore

        stage = "readiness" if prepared else "prepare"
        original = getattr(service.NativeTestingRebuildExecution, stage)
        save = RecordTestingRebuildStore.save
        captured: list[TestingRebuildRecord] = []

        def remembered(store: Any, record: TestingRebuildRecord) -> None:
            save(store, record)
            captured.append(record)

        def interrupted(executor: Any, plan: Any) -> Any:
            if not prepared:
                original(executor, plan)
            # This fixture already created/configured the independent candidate.
            # Reconstruct the original executor's persisted dispatch boundary,
            # then lose its reply before the preparation receipt is saved.
            executor.lease.checkpoint_effect("candidate_configuration")
            raise NativeSourceUnavailable("Fixture lost the private preparation reply")

        with (
            patch.object(service.NativeTestingRebuildExecution, stage, interrupted),
            patch.object(RecordTestingRebuildStore, "save", remembered),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        self.assertTrue(service.native_rebuild_effects_started(self.store, self.operation))
        self.assertEqual(self.effects, [])
        self.admission_checkpoint = captured[-1]

    def assert_admission_changed_before_cutover(self) -> TestingRebuildRecord:
        from control_plane.testing_rebuild_storage import RecordTestingRebuildStore

        save = RecordTestingRebuildStore.save
        captured: list[TestingRebuildRecord] = []

        def remembered(store: Any, record: TestingRebuildRecord) -> None:
            save(store, record)
            captured.append(record)

        before = self.store.read_testing_candidate(self.fixture.candidate.namespace)
        with patch.object(RecordTestingRebuildStore, "save", remembered):
            result = self.execute()
        self.assertEqual(result.deploy_status, "fail")
        self.assertEqual(result.error_code, "testing_rebuild_admission_changed")
        self.assertTrue(result.error_message)
        self.assertEqual(self.current().target_id, self.fixture.before.target_id)
        self.assertEqual(
            self.store.read_testing_candidate(self.fixture.candidate.namespace), before
        )
        self.assertEqual(self.effects, [])
        self.assertFalse(self.retained["stopped"])
        self.assertEqual(self.execute(), result)
        failed = captured[-1]
        self.assertEqual(failed.step, "failed")
        self.assertEqual(failed.preparation, self.admission_checkpoint.preparation)
        self.assertEqual(failed.admission_refusal, result.error_message)
        return failed

    def test_profile_drift_after_candidate_effects_ends_before_cutover(self) -> None:
        self.hold_after_private_candidate_effects(prepared=True)
        profile = self.store.read_product_profile_record(self.operation.product)
        assert profile.runtime_port is not None
        self.store.write_product_profile_record(
            profile.model_copy(update={"runtime_port": profile.runtime_port + 1})
        )
        failed = self.assert_admission_changed_before_cutover()
        self.assertIsNotNone(failed.preparation)
        for flag in ("publication_started", "retirement_started", "rollback_started"):
            with self.subTest(flag=flag), self.assertRaises(ValueError):
                TestingRebuildRecord.model_validate({**failed.model_dump(), flag: True})

    def test_missing_candidate_cannot_save_a_terminal_admission_checkpoint(self) -> None:
        from control_plane.testing_rebuild_storage import RecordTestingRebuildStore

        self.hold_after_private_candidate_effects()
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"runtime_port": profile.runtime_port + 1})
        )
        with (
            patch.object(self.store, "read_testing_candidate", return_value=None),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        self.assertEqual(
            RecordTestingRebuildStore(self.store).load(self.admission_checkpoint.plan),
            self.admission_checkpoint,
        )
        self.assertEqual(self.effects, [])

    def test_recovery_reports_protected_resources_without_reclassifying_admission(self) -> None:
        from tests.support.disposable_lanes import record_safety

        executor = service.NativeTestingRebuildExecution(
            service.NativeTestingRebuildAdapter(
                self.root, self.store, self.operation, self.intent, self.authorize
            ),
            Mock(spec=["assert_current"]),
            Mock(),
        )
        executor.host, executor.token = "fixture-host", "fixture-token"
        for changes in (
            {"data": "only_copy"},
            {"production_resources": "shared"},
            {"production_resources": "separation_ready"},
        ):
            with self.subTest(changes=changes):
                record_safety(
                    self.store,
                    self.operation.product,
                    self.operation.context,
                    "testing",
                    **changes,
                )
                with self.assertRaises(service.NativeTestingRebuildPending):
                    executor.guard(recovery=True)
                self.assertEqual(self.effects, [])
                self.assertEqual(self.current().target_id, self.fixture.before.target_id)

    def test_recovery_names_unverified_candidate_custody_without_releasing_it(self) -> None:
        candidate = self.fixture.candidate
        provider = self.store.read_provider_target_record(
            context_name=candidate.namespace, instance_name="testing-candidate"
        )
        self.store.write_provider_target_record(
            provider.model_copy(update={"target_id": "foreign"})
        )
        executor = service.NativeTestingRebuildExecution(
            service.NativeTestingRebuildAdapter(
                self.root, self.store, self.operation, self.intent, self.authorize
            ),
            Mock(spec=["assert_current"]),
            Mock(),
        )
        executor.host, executor.token = "fixture-host", "fixture-token"
        with self.assertRaises(service.NativeTestingRebuildPending):
            executor.guard(recovery=True)
        self.assertEqual(self.store.read_testing_candidate(candidate.namespace), candidate)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current().target_id, self.fixture.before.target_id)

    def test_unavailable_production_probe_is_not_permanent_admission_drift(self) -> None:
        from control_plane import testing_candidate_runtime as compiler
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        operation = self.operation.model_copy(
            update={
                "request": self.operation.request.model_copy(
                    update={"data_source_mode": "production_restore", "allow_empty_data": False},
                )
            }
        )
        allocation = self.fixture.candidate.model_copy(
            update={
                "application": None,
                "intent_sha256": compiler.candidate_application_intent(
                    product=operation.product,
                    context=operation.context,
                    artifact_id=operation.request.artifact_id,
                    source="production",
                ),
            }
        )
        executor = service.NativeTestingRebuildExecution(
            service.NativeTestingRebuildAdapter(
                self.root, self.store, operation, self.intent, self.authorize
            ),
            Mock(),
            Mock(),
        )
        unavailable = NativeSourceUnavailable("Fixture source handshake unavailable")
        with (
            patch.object(compiler, "native_testing_source_authority", return_value=Mock()),
            patch.object(
                compiler, "resolve_native_testing_source", side_effect=unavailable
            ) as probe,
            self.assertRaises(NativeSourceUnavailable) as caught,
        ):
            executor.current_candidate_inputs(allocation)
        self.assertIs(caught.exception, unavailable)
        probe.assert_called_once()
        self.assertTrue(probe.call_args.kwargs["probe"])
        self.assertEqual(self.effects, [])

    def test_input_store_failure_is_not_relabelled_as_permanent_admission_drift(self) -> None:
        self.hold_after_private_candidate_effects()
        with (
            patch.object(
                service,
                "candidate_runtime_inputs",
                side_effect=ValueError("Fixture metadata read unavailable"),
            ),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        self.assertEqual(self.effects, [])

    def test_admission_drift_preserves_unknown_allocation_custody(self) -> None:
        candidate = self.fixture.candidate.model_copy(
            update={
                "application": None,
                "compose_id": "",
                "effect": "compose_id",
                "resources_binding_sha256": "",
            }
        )
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"runtime_port": profile.runtime_port + 1})
        )
        with (
            patch.object(self.store, "read_testing_candidate", return_value=candidate),
            patch.object(service, "observe_candidate_creation", return_value=None) as observe,
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        observe.assert_called_once()
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current().target_id, self.fixture.before.target_id)

    def test_admission_drift_observes_original_storage_without_preparing_again(self) -> None:
        from control_plane.storage.postgres import LaunchplaneTestingCandidateRow
        from control_plane import testing_candidate_allocation as allocation_service

        candidate = self.fixture.candidate.model_copy(
            update={"application": None, "resources_binding_sha256": ""}
        )
        with self.store._session_factory.begin() as session:
            row = session.get(LaunchplaneTestingCandidateRow, candidate.namespace)
            assert row is not None
            row.payload = candidate.model_dump(mode="json", exclude_none=True)
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": tuple(
                        lane for lane in profile.lanes if lane.context != self.operation.context
                    )
                }
            )
        )
        with (
            patch.object(
                allocation_service,
                "run_resource_schedule",
                side_effect=candidate_application.CandidateObservationUnavailable("Fixture busy"),
            ) as readback,
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        self.assertEqual(self.store.read_testing_candidate(candidate.namespace), candidate)
        with patch.object(allocation_service, "run_resource_schedule", return_value={}) as done:
            result = self.execute()
        self.assertEqual(readback.call_args.kwargs["mode"], "readback")
        self.assertEqual(done.call_args.kwargs["mode"], "readback")
        observed = self.store.read_testing_candidate(candidate.namespace)
        assert observed is not None
        self.assertEqual(
            observed.resources_binding_sha256, candidate_resources(candidate).binding_sha256
        )
        self.assertEqual(result.error_code, "testing_rebuild_admission_changed")
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current().target_id, self.fixture.before.target_id)

    def test_admission_drift_observes_exact_original_configuration_without_new_inputs(self) -> None:
        from control_plane.storage.postgres import LaunchplaneTestingCandidateRow

        assert self.app.configuration_sha256
        app = self.app.model_copy(
            update={
                "phase": "configuring",
                "deployment_id": "",
                "prepared": None,
                "restore_schedule_id": "",
                "restore_previous_deployments": (),
                "restore_workflow_started": None,
                "workflow_evidence": {},
            }
        )
        candidate = self.fixture.candidate.model_copy(update={"application": app})
        with self.store._session_factory.begin() as session:
            row = session.get(LaunchplaneTestingCandidateRow, candidate.namespace)
            assert row is not None
            row.payload = candidate.model_dump(mode="json", exclude_none=True)
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"runtime_port": profile.runtime_port + 1})
        )
        provider = self.fixture.fixture.provider_state
        original = dict(provider)
        provider["env"] = "UNEXPECTED=value"
        with self.assertRaises(service.NativeTestingRebuildPending):
            self.execute()
        self.assertEqual(self.store.read_testing_candidate(candidate.namespace), candidate)
        provider.clear()
        provider.update(original)
        with patch.object(
            service,
            "candidate_runtime_inputs",
            side_effect=AssertionError("Do not select new inputs"),
        ):
            result = self.execute()
        observed = self.store.read_testing_candidate(candidate.namespace)
        assert observed is not None and observed.application is not None
        self.assertEqual(observed.application.phase, "abandoned")
        self.assertEqual(
            observed.application.abandonment_evidence, app.retained_configuration_evidence
        )
        self.assertEqual(observed.application.configuration_sha256, app.configuration_sha256)
        self.assertEqual(observed.application.plan, app.plan)
        with self.assertRaises(ValueError):
            self.store.compare_and_write_testing_candidate(
                observed,
                observed.model_copy(
                    update={
                        "application": observed.application.model_copy(
                            update={"phase": "deploying"}
                        )
                    }
                ),
                observation_only=True,
            )
        self.assertEqual(self.store.read_testing_candidate(candidate.namespace), observed)
        self.assertFalse(result.runtime_identity_injected)
        self.assertEqual(result.target_id, self.intent.old.target_id)
        self.assertTrue(result.error_code)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current().target_id, self.fixture.before.target_id)

    def empty_original_configuration(self, *, legacy: bool = False) -> TestingCandidateAllocation:
        from control_plane.storage.postgres import LaunchplaneTestingCandidateRow

        app = self.app.model_copy(
            update={
                "phase": "configuring",
                "configuration_sha256": "" if legacy else self.app.configuration_sha256,
                "deployment_id": "",
                "prepared": None,
                "restore_schedule_id": "",
                "restore_previous_deployments": (),
                "restore_workflow_started": None,
                "workflow_evidence": {},
            }
        )
        candidate = self.fixture.candidate.model_copy(update={"application": app})
        with self.store._session_factory.begin() as session:
            row = session.get(LaunchplaneTestingCandidateRow, candidate.namespace)
            assert row is not None
            row.payload = candidate.model_dump(mode="json", exclude_none=True)
        provider = self.fixture.fixture.provider_state
        provider.clear()
        provider.update(
            composeId=candidate.compose_id,
            environmentId=candidate.environment_id,
            serverId=candidate.server_id,
            name=candidate.namespace,
            appName=candidate.namespace + "-a1b2c3",
            sourceType="raw",
            composeFile="",
            env="",
            autoDeploy=False,
        )
        self.fixture.fixture.effects.clear()
        return candidate

    def test_unapplied_private_configuration_fails_without_holding_the_lane(self) -> None:
        configured = dict(self.fixture.fixture.provider_state)
        candidate = self.empty_original_configuration()
        result = self.execute()
        self.assert_failed_checkpoint(result)
        observed = self.store.read_testing_candidate(candidate.namespace)
        assert observed is not None and observed.application is not None
        self.assertEqual(observed.application.phase, "abandoned")
        self.assertTrue(observed.application.abandonment_evidence)
        self.assertEqual(observed.application.configuration_sha256, self.app.configuration_sha256)
        self.assertEqual(observed.application.plan, self.app.plan)
        self.assertEqual(self.current(), self.fixture.before)
        self.assertEqual(self.effects, [])
        self.assertFalse(self.retained["stopped"])
        # A delayed original update can configure only the retained private copy.
        self.fixture.fixture.provider_state.update(configured)
        self.assertEqual(self.execute(), result)
        self.assertEqual(self.store.read_testing_candidate(candidate.namespace), observed)
        self.assertEqual(self.fixture.fixture.effects, [])
        self.assertEqual(self.current(), self.fixture.before)
        with self.assertRaises(ValueError):
            candidate_application.prepare_candidate_application(
                root=self.root,
                store=self.store,
                allocation=observed,
                inputs=self.fixture.fixture.compiled,
                lease=Mock(spec=["assert_current"]),
                guard=Mock(),
            )
        self.assertEqual(self.fixture.fixture.effects, [])

    def test_admission_loss_can_retain_an_empty_legacy_configuration(self) -> None:
        candidate = self.empty_original_configuration(legacy=True)
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"runtime_port": profile.runtime_port + 1})
        )
        result = self.execute()
        self.assertFalse(result.runtime_identity_injected)
        self.assertEqual(result.target_id, self.intent.old.target_id)
        self.assertTrue(result.error_code)
        observed = self.store.read_testing_candidate(candidate.namespace)
        assert observed is not None and observed.application is not None
        self.assertEqual(observed.application.phase, "abandoned")
        self.assertFalse(observed.application.configuration_sha256)
        self.assertEqual(observed.application.plan, self.app.plan)
        self.assertEqual(self.current(), self.fixture.before)
        self.assertEqual(self.effects, [])

    def retain_after_secret_rotation(self, *, phase: str) -> None:
        from control_plane.storage.postgres import LaunchplaneTestingCandidateRow

        configured = dict(self.fixture.fixture.provider_state)
        candidate = self.empty_original_configuration()
        assert candidate.application is not None
        candidate = candidate.model_copy(
            update={"application": candidate.application.model_copy(update={"phase": phase})}
        )
        with self.store._session_factory.begin() as session:
            row = session.get(LaunchplaneTestingCandidateRow, candidate.namespace)
            assert row is not None
            row.payload = candidate.model_dump(mode="json", exclude_none=True)
        self.fixture.fixture.provider_state.clear()
        self.fixture.fixture.provider_state.update(configured)
        versions = {
            item.secret_id: item.current_version_id
            for item in self.store.list_secret_records(
                context_name=candidate.context, instance_name="testing"
            )
        }
        self.store.write_product_authority_bundle(
            generation_bundle(
                self.store,
                self.intent.profile,
                candidate.context,
                actor="fixture",
                generation="rotated-before-build",
                only_keys=frozenset({"ODOO_DB_PASSWORD"}),
            )
        )
        self.assertNotEqual(
            {
                item.secret_id: item.current_version_id
                for item in self.store.list_secret_records(
                    context_name=candidate.context, instance_name="testing"
                )
            },
            versions,
        )
        result = self.execute()
        self.assertFalse(result.runtime_identity_injected)
        self.assertEqual(result.target_id, self.intent.old.target_id)
        self.assertTrue(result.error_code)
        self.assertEqual(self.execute(), result)
        observed = self.store.read_testing_candidate(candidate.namespace)
        assert observed is not None and observed.application is not None
        assert candidate.application is not None
        self.assertEqual(observed.application.phase, "abandoned")
        self.assertEqual(observed.application.plan, candidate.application.plan)
        self.assertEqual(
            observed.application.configuration_sha256, candidate.application.configuration_sha256
        )
        self.assertEqual(
            observed.application.abandonment_evidence,
            candidate.application.retained_configuration_evidence,
        )
        with self.assertRaises(ValueError):
            self.store.compare_and_write_testing_candidate(
                candidate,
                candidate.model_copy(
                    update={
                        "application": candidate.application.model_copy(
                            update={"phase": "deploying"}
                        )
                    }
                ),
            )
        self.assertEqual(self.store.read_testing_candidate(candidate.namespace), observed)
        self.assertEqual(self.fixture.fixture.provider_state, configured)
        self.assertEqual(self.current(), self.fixture.before)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.fixture.fixture.effects, [])

    def test_secret_rotation_retains_original_configured_copy_without_deployment(self) -> None:
        self.retain_after_secret_rotation(phase="configured")

    def test_secret_rotation_retains_applied_configuration_after_lost_update_reply(self) -> None:
        self.retain_after_secret_rotation(phase="configuring")

    def test_lost_empty_configuration_observation_reply_reuses_its_abandonment(self) -> None:
        candidate = self.empty_original_configuration()
        write = self.store.compare_and_write_testing_candidate
        lost = False

        def lose_reply(expected: Any, replacement: Any, **kwargs: Any) -> None:
            nonlocal lost
            write(expected, replacement, **kwargs)
            if replacement.application.phase == "abandoned" and not lost:
                lost = True
                raise TimeoutError("Fixture lost observation commit reply")

        with (
            patch.object(self.store, "compare_and_write_testing_candidate", side_effect=lose_reply),
            self.assertRaises(TimeoutError),
        ):
            self.execute()
        retained = self.store.read_testing_candidate(candidate.namespace)
        assert retained is not None and retained.application is not None
        self.assertEqual(retained.application.phase, "abandoned")
        self.assert_failed_checkpoint(self.execute())
        self.assertEqual(self.store.read_testing_candidate(candidate.namespace), retained)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current(), self.fixture.before)

    def test_configuration_abandonment_receipt_must_bind_original_intent(self) -> None:
        candidate = self.empty_original_configuration()
        app = candidate.application
        assert app is not None
        replacement = candidate.model_copy(
            update={
                "application": app.model_copy(
                    update={"phase": "abandoned", "abandonment_evidence": "foreign-observation"}
                )
            }
        )
        with self.assertRaises(ValueError):
            self.store.compare_and_write_testing_candidate(
                candidate, replacement, observation_only=True
            )
        self.assertEqual(self.store.read_testing_candidate(candidate.namespace), candidate)
        self.assertEqual(self.effects, [])

    def test_configuration_abandonment_requires_empty_unexecuted_private_custody(self) -> None:
        for change in ("env", "source", "placement", "name", "appName", "domain", "deployment"):
            with self.subTest(change=change):
                candidate = self.empty_original_configuration()
                payload = self.fixture.fixture.provider_state
                if change == "env":
                    payload["env"] = "UNEXPECTED=value"
                elif change == "source":
                    payload["repository"] = "foreign-source"
                elif change == "placement":
                    payload["serverId"] = "foreign-server"
                elif change == "name":
                    payload["name"] = "foreign-copy"
                elif change == "appName":
                    payload["appName"] = "foreign-copy"
                with (
                    patch.object(
                        candidate_application,
                        "fetch_dokploy_compose_domains_for_target_setup",
                        return_value=("unexpected-domain",) if change == "domain" else (),
                    ),
                    patch.object(
                        api,
                        "list_deployments_for_target",
                        side_effect=lambda **kwargs: (
                            [{"deploymentId": "unexpected", "status": "running"}]
                            if change == "deployment"
                            and kwargs["target_id"] == candidate.compose_id
                            else []
                        ),
                    ),
                    self.assertRaises(service.NativeTestingRebuildPending),
                ):
                    self.execute()
                self.assertEqual(self.store.read_testing_candidate(candidate.namespace), candidate)
                self.assertEqual(self.current(), self.fixture.before)
                self.assertEqual(self.effects, [])

    def test_empty_configuration_failure_finishes_the_worker_and_admits_a_fresh_request(
        self,
    ) -> None:
        from control_plane.contracts.durable_operation_authorization import (
            DurableOperationAuthorization,
        )
        from control_plane.workflows import odoo_stable_operation_worker as worker
        from tests.support.durable_operations import (
            durable_operation_authorization_payload,
            durable_operation_policy_record,
        )

        candidate = self.empty_original_configuration()
        authorization = durable_operation_authorization_payload(
            action="odoo_target_replacement_apply.execute",
            managed_rule_id="native-testing-fixture",
            product=self.operation.product,
            context=self.operation.context,
        )
        now = datetime.now(UTC)
        operation = self.operation.model_copy(
            update={
                "schema_version": 2,
                "authorization": DurableOperationAuthorization.model_validate(authorization),
                "status": "running",
                "phase": "running",
                "lease_owner": "worker",
                "started_at": now.isoformat(),
                "lease_expires_at": (now + timedelta(minutes=5)).isoformat(),
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(operation)
        with patch.object(
            worker,
            "read_active_authz_policy_record",
            return_value=durable_operation_policy_record(authorization),
        ):
            self.assertTrue(
                worker._execute_target_replacement_with_checkpoint(
                    record_store=self.store,
                    control_plane_root_path=self.root,
                    operation=operation,
                    lease_owner="worker",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )
            )
        failed = self.store.read_odoo_stable_target_replacement_operation_record(
            operation.operation_id
        )
        self.assertEqual(failed.status, "fail")
        self.assertTrue(failed.finished_at)
        fresh = self.operation.model_copy(
            update={
                "operation_id": operation.operation_id + "-successor",
                "idempotency_key": "successor-request",
                "request_fingerprint": "successor-fingerprint",
            }
        )
        queued, created = (
            self.store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
                fresh
            )
        )
        self.assertTrue(created)
        self.assertEqual(queued.operation_id, fresh.operation_id)
        self.assertNotEqual(
            testing_candidate_namespace(fresh.product, fresh.context, fresh.operation_id),
            candidate.namespace,
        )
        retained = self.store.read_testing_candidate(candidate.namespace)
        assert retained is not None and retained.application is not None
        self.assertEqual(retained.application.phase, "abandoned")
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current(), self.fixture.before)

    def test_terminal_incomplete_storage_ends_admission_without_another_preparation(self) -> None:
        from control_plane.storage.postgres import LaunchplaneTestingCandidateRow
        from control_plane import testing_candidate_allocation as allocation_service
        from control_plane.testing_replacement_resources import IncompleteTestingResources

        candidate = self.fixture.candidate.model_copy(
            update={"application": None, "resources_binding_sha256": ""}
        )
        with self.store._session_factory.begin() as session:
            row = session.get(LaunchplaneTestingCandidateRow, candidate.namespace)
            assert row is not None
            row.payload = candidate.model_dump(mode="json", exclude_none=True)
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"runtime_port": profile.runtime_port + 1})
        )
        with patch.object(
            allocation_service,
            "run_resource_schedule",
            side_effect=IncompleteTestingResources("Fixture terminal empty storage"),
        ) as readback:
            result = self.execute()
        self.assertEqual(readback.call_args.kwargs["mode"], "readback")
        self.assertEqual(result.error_code, "testing_rebuild_admission_changed")
        self.assertEqual(self.store.read_testing_candidate(candidate.namespace), candidate)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current().target_id, self.fixture.before.target_id)

    def test_execution_receipts_prevent_configuration_only_abandonment(self) -> None:
        app = self.app.model_copy(
            update={
                "configuration_sha256": "",
                "phase": "configuring",
                "prepared": None,
                "restore_workflow_started": None,
            }
        )
        candidate = self.fixture.candidate.model_copy(update={"application": app})
        with (
            patch.object(api, "fetch_dokploy_target_payload") as read,
            self.assertRaises(candidate_application.CandidateObservationUnavailable),
        ):
            candidate_application.observe_candidate_configuration(
                root=self.root,
                store=self.store,
                record=candidate,
                lease=Mock(spec=["assert_current"]),
                guard=Mock(),
            )
        read.assert_not_called()
        self.assertEqual(self.effects, [])

    def test_admission_drift_cannot_outlive_an_original_candidate_job(self) -> None:
        for phase in ("configuring", "deploying", "restoring"):
            with self.subTest(phase=phase):
                app = self.app.model_copy(update={"phase": phase, "prepared": None})
                candidate = self.fixture.candidate.model_copy(update={"application": app})
                profile = self.store.read_product_profile_record(self.operation.product)
                self.store.write_product_profile_record(
                    profile.model_copy(update={"runtime_port": profile.runtime_port + 1})
                )
                with (
                    patch.object(self.store, "read_testing_candidate", return_value=candidate),
                    patch.object(
                        service,
                        "candidate_terminal_custody_evidence",
                        side_effect=candidate_application.CandidateObservationUnavailable(
                            "Fixture original job is running"
                        ),
                    ) as observe,
                    self.assertRaises(service.NativeTestingRebuildPending),
                ):
                    self.execute()
                self.assertEqual(observe.call_count, 0 if phase == "configuring" else 1)
                self.assertEqual(self.effects, [])
                self.assertFalse(self.retained["stopped"])

        with (
            patch.object(self.store, "read_testing_candidate", return_value=candidate),
            patch.object(
                service,
                "candidate_terminal_custody_evidence",
                return_value="fixture-original-job:done",
            ) as terminal,
        ):
            result = self.execute()
        terminal.assert_called_once()
        self.assertEqual(result.error_code, "testing_rebuild_admission_changed")
        self.assertEqual(self.effects, [])

    def test_terminal_admission_receipt_cannot_replay_under_another_intent(self) -> None:
        self.hold_after_private_candidate_effects()
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"runtime_port": profile.runtime_port + 1})
        )
        self.assert_admission_changed_before_cutover()
        changed = self.intent.model_copy(update={"project": self.intent.project + "-changed"})
        executor = service.NativeTestingRebuildExecution(
            service.NativeTestingRebuildAdapter(
                self.root, self.store, self.operation, changed, self.authorize
            ),
            Mock(),
            Mock(),
        )
        with self.assertRaises(ValueError):
            executor.run()
        self.assertEqual(self.effects, [])

    def test_profile_drift_after_publication_does_not_strand_old_copy_retirement(self) -> None:
        publish = publication_write

        def changed(*args: Any, **kwargs: Any) -> Any:
            result = publish(*args, **kwargs)
            profile = self.store.read_product_profile_record(self.operation.product)
            self.store.write_product_profile_record(
                profile.model_copy(update={"runtime_port": profile.runtime_port + 1})
            )
            return result

        with patch.object(service, "publish", side_effect=changed):
            self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])
        self.assertEqual(self.current().target_id.target_id, self.fixture.candidate.compose_id)
        with self.assertRaises(ValueError):
            published_testing_copy(
                self.store, product=self.operation.product, context=self.operation.context
            )
        with self.assertRaises(ValueError):
            published_testing_copy(
                self.store,
                product=self.operation.product,
                context=self.operation.context,
                recovery_operation_id=self.operation.operation_id + "-foreign",
            )

    def test_runtime_edit_after_candidate_effects_has_a_finite_result(self) -> None:
        self.hold_after_private_candidate_effects()
        runtime = self.current().runtime
        assert runtime is not None
        self.store.write_runtime_environment_record(
            runtime.model_copy(
                update={
                    "env": {**runtime.env, "ODOO_ADMIN_LOGIN": "new-testing-login"},
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
        )
        self.assert_admission_changed_before_cutover()

    def test_context_loss_after_candidate_effects_ends_without_route_dispatch(self) -> None:
        self.hold_after_private_candidate_effects()
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": tuple(
                        lane for lane in profile.lanes if lane.context != self.operation.context
                    )
                }
            )
        )
        self.assert_admission_changed_before_cutover()

    def test_retired_testing_lane_after_effects_has_a_finite_result(self) -> None:
        self.hold_after_private_candidate_effects()
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": tuple(
                        lane
                        for lane in profile.lanes
                        if (lane.context, lane.instance) != (self.operation.context, "testing")
                    ),
                    "historical_contexts": tuple(
                        sorted({*profile.historical_contexts, self.operation.context})
                    ),
                }
            )
        )
        self.assert_admission_changed_before_cutover()

    def test_changed_testing_credentials_before_cutover_preserve_candidate_custody(self) -> None:
        from control_plane.testing_local_credentials import generation_bundle
        from control_plane.odoo_testing_restore import FRESH_TESTING_LOCAL_CREDENTIAL_KEYS

        self.hold_after_private_candidate_effects()
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_authority_bundle(
            generation_bundle(
                self.store,
                profile,
                self.operation.context,
                actor="fixture-admin",
                generation="changed-fixture-generation",
                only_keys=FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
            )
        )
        self.assert_admission_changed_before_cutover()

    def test_admission_failure_with_partial_application_survives_a_lost_checkpoint_reply(
        self,
    ) -> None:
        from control_plane.testing_rebuild_storage import RecordTestingRebuildStore

        self.hold_after_private_candidate_effects()
        candidate = self.store.read_testing_candidate(self.fixture.candidate.namespace)
        assert candidate is not None
        partial = candidate.model_copy(update={"application": None})
        read = self.store.read_testing_candidate
        save = RecordTestingRebuildStore.save
        profile = self.store.read_product_profile_record(self.operation.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"runtime_port": profile.runtime_port + 1})
        )

        def candidate_read(namespace: str) -> Any:
            return partial if namespace == partial.namespace else read(namespace)

        def lost_reply(store: Any, record: TestingRebuildRecord) -> None:
            save(store, record)
            if record.admission_refusal:
                raise RuntimeError("Fixture lost a committed admission-failure reply")

        with patch.object(self.store, "read_testing_candidate", side_effect=candidate_read):
            with patch.object(RecordTestingRebuildStore, "save", lost_reply):
                with self.assertRaises(service.NativeTestingRebuildPending):
                    self.execute()
            with patch.object(
                DokployTestingRouteTransport,
                "publish",
                side_effect=AssertionError("A terminal refusal never dispatches"),
            ):
                result = self.execute()
        self.assertEqual(result.error_code, "testing_rebuild_admission_changed")
        self.assertEqual(result.artifact_id, self.operation.request.artifact_id)
        self.assertTrue(result.error_message)
        self.assertEqual(self.store.read_testing_candidate(candidate.namespace), candidate)
        self.assertEqual(self.effects, [])
        self.assertFalse(self.retained["stopped"])

    def test_new_hold_after_healthy_cutover_records_reality_and_preserves_old_copy(self) -> None:
        hold: Any = None

        def held(plan: Any, backend: str, **kwargs: Any) -> str:
            nonlocal hold
            self.switch(plan, backend, **kwargs)
            hold = self.apply_staff_hold(True, "Staff hold after cutover")
            return "applied"

        with (
            patch.object(DokployTestingRouteTransport, "publish", side_effect=held),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        self.assertEqual(self.current().target_id.target_id, self.fixture.candidate.compose_id)
        self.assertIsNotNone(hold)
        self.assertEqual(self.current().target.policies.staff_testing_hold, hold)
        self.assertEqual(self.effects, ["switch-new"])
        self.assertFalse(self.retained["stopped"])
        self.apply_staff_hold(False, "Staff testing complete")
        self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def test_hold_after_interrupted_cutover_still_allows_original_inverse_on_resume(self) -> None:
        from control_plane.contracts.promotion_record import HealthcheckEvidence

        def health(base_url: str, path: str) -> HealthcheckEvidence:
            if self.owned is not None:
                return HealthcheckEvidence(status="pending", urls=(base_url,))
            return self.health(base_url, path)

        with (
            patch.object(service, "read_public_testing_health", side_effect=health),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        hold = self.apply_staff_hold(True, "Staff hold during the interrupted cutover")
        self.fail_public = True
        self.assertEqual(self.execute().deploy_status, "fail")
        self.assertEqual(self.effects, ["switch-new", "switch-old"])
        self.assertEqual(self.current().target.policies.staff_testing_hold, hold)
        self.assertFalse(self.retained["stopped"])

    def test_legacy_refresh_during_first_rebuild_preserves_preparation_and_publication(
        self,
    ) -> None:
        from tests.test_route_binding_refresh_controller import _service_binding

        candidate = self.fixture.candidate
        binding = _service_binding().model_copy(
            update={"product": candidate.product, "context": candidate.context}
        )
        binding = binding.model_copy(
            update={
                "domains": (
                    binding.domains[0].model_copy(update={"domain_name": self.fixture.route.host}),
                )
            }
        )
        self.store.write_route_binding_record(binding)
        self.fixture.before = self.current()
        self.intent = self.intent.model_copy(update={"before": self.fixture.before})
        self.capture.return_value = self.intent
        prepare = service.NativeTestingRebuildExecution.prepare
        switch = self.switch

        def refresh_evidence() -> None:
            current = self.current().binding
            assert current is not None
            self.store.write_route_binding_record(
                current.model_copy(
                    update={
                        "source": current.source.model_copy(
                            update={
                                "refreshed_at": datetime.now(UTC).isoformat(),
                                "source_versions": {
                                    **current.source.source_versions,
                                    "observation": "new-evidence",
                                },
                            }
                        ),
                        "updated_at": datetime.now(UTC).isoformat(),
                    }
                )
            )

        def interleave_prepare(execution: Any, *args: Any, **kwargs: Any) -> Any:
            refresh_evidence()
            return prepare(execution, *args, **kwargs)

        def interleave_switch(*args: Any, **kwargs: Any) -> Any:
            result = switch(*args, **kwargs)
            refresh_evidence()
            return result

        with (
            patch.object(service.NativeTestingRebuildExecution, "prepare", new=interleave_prepare),
            patch.object(DokployTestingRouteTransport, "publish", side_effect=interleave_switch),
        ):
            self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def refresh_through_http(self, key: str) -> dict[str, Any]:
        import asyncio
        from tests.test_native_testing_route_refresh import refresh_testing_binding_http

        from control_plane.contracts.authz_policy_record import (
            LaunchplaneAuthzPolicyRecord,
            authz_policy_sha256,
            build_authz_policy_record_id,
        )
        from tests.test_route_binding_refresh_controller import _controller_policy

        # Only refresh scenarios own this policy. Other worker scenarios seed
        # their own identities, including the cancellation boundary.
        policy = _controller_policy(
            mode="apply", products=(self.operation.product,), contexts=(self.operation.context,)
        )
        digest = authz_policy_sha256(policy)
        seeded = self.store.seed_authz_policy_if_absent(
            LaunchplaneAuthzPolicyRecord(
                record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
                source="test:native-testing-refresh",
                updated_at=self.operation.updated_at,
                policy=policy,
            )
        )
        self.assertEqual(seeded.policy_sha256, digest)

        current = self.current()
        publication = published_testing_copy(
            self.store, product=self.operation.product, context=self.operation.context
        )
        assert publication is not None and current.inventory is not None
        assert current.inventory.runtime_identity is not None
        frame = {
            "raw": deepcopy(self.raw),
            "owned_file": json.dumps(self.owned),
            "old_endpoints": (publication.plan.route.new_backend,),
            "old_identity": current.inventory.runtime_identity.model_dump(mode="json"),
        }
        with patch.object(DokployTestingRouteTransport, "read", return_value=frame):
            response = asyncio.run(
                refresh_testing_binding_http(
                    self.store,
                    self.root,
                    self.operation.product,
                    self.operation.context,
                    datetime.now(UTC),
                    key,
                )
            )
        self.assertEqual(response.status_code, 202, response.text)
        return dict(response.json()["result"])

    def test_refresh_between_publication_and_retirement_does_not_interrupt_rebuild(self) -> None:
        publish = service.NativeTestingRebuildExecution.publish

        def interleave(execution: Any, *args: Any, **kwargs: Any) -> None:
            publish(execution, *args, **kwargs)
            result = self.refresh_through_http("during-publication")
            self.assertEqual(result["refreshed_count"], 1, result)

        with patch.object(service.NativeTestingRebuildExecution, "publish", new=interleave):
            self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def test_second_rebuild_uses_published_copy_and_preserves_later_deployment_route(self) -> None:
        self.complete_second_rebuild(refresh_during_prepare=True)
        before = self.current()
        result = self.refresh_through_http("after-second-rebuild")
        self.assertEqual(result["refreshed_count"], 1, result)
        result = self.refresh_through_http("after-second-rebuild-again")
        self.assertEqual(result["unchanged_count"], 1, result)
        self.assertEqual(self.current().binding.domains, before.binding.domains)
        self.assertEqual(self.current().target_id, before.target_id)

    def test_next_rebuild_moves_to_desired_port_without_reverting_the_profile(self) -> None:
        self.complete_second_rebuild(change_port=True)

    def complete_second_rebuild(
        self, *, change_port: bool = False, refresh_during_prepare: bool = False
    ) -> None:
        import yaml

        self.assertEqual(self.execute().deploy_status, "pass")
        first = self.fixture.candidate
        previous = published_testing_copy(self.store, product=first.product, context=first.context)
        assert previous is not None
        if change_port:
            profile = self.store.read_product_profile_record(first.product)
            self.store.write_product_profile_record(
                profile.model_copy(update={"runtime_port": profile.runtime_port + 1})
            )
            with self.assertRaises(ValueError):
                published_testing_copy(self.store, product=first.product, context=first.context)
        before = self.current()
        secrets = self.store.list_secret_records()
        operation_id = "second-native-rebuild"
        namespace = testing_candidate_namespace(first.product, first.context, operation_id)
        candidate = first.model_copy(
            update={
                "operation_id": operation_id,
                "namespace": namespace,
                "serving_target_id": first.compose_id,
                "project_id": "second-project",
                "environment_id": "second-environment",
                "compose_id": "second-compose",
                "application": None,
                "resources_binding_sha256": "",
            }
        )
        candidate = candidate.model_copy(
            update={"resources_binding_sha256": candidate_resources(candidate).binding_sha256}
        )
        self.store.compare_and_write_testing_candidate(None, candidate)
        inputs = candidate_runtime_inputs(
            root=self.root,
            store=self.store,
            allocation=candidate,
            artifact_id=self.operation.request.artifact_id,
            source="empty",
        )
        app_fixture = self.fixture.fixture
        app_fixture.allocation, app_fixture.compiled = candidate, inputs
        app_fixture.fixture.allocation = candidate
        app_fixture.fixture.before = app_fixture.fixture.authority()
        app_fixture.effects.clear()
        app_fixture.provider_state.update(
            composeId=candidate.compose_id,
            environmentId=candidate.environment_id,
            name=namespace,
            appName=namespace,
            composeFile="",
            env="",
        )
        app_fixture.deployments.clear()
        app_fixture.restore_deployments.clear()
        app_fixture.restored = False
        self.original_identity = self.app.plan.runtime_identity
        self.retained.update(
            identity=self.original_identity.model_dump(mode="json"),
            resource_binding="first-copy-data",
            physical_binding="first-copy-backing",
            stopped=False,
            web_stopped=False,
        )
        self.old_provider.update(
            composeId=first.compose_id, name=first.namespace, appName=first.namespace
        )
        self.intent = self.intent.model_copy(
            update={
                "before": before,
                "project": first.namespace,
                "old": TestingCopy(
                    copy_id=first.compose_id,
                    target_id=first.compose_id,
                    private_url=previous.plan.route.new_backend,
                    resource_binding=self.retained["physical_binding"],
                    runtime_identity=self.original_identity,
                ),
            }
        )
        self.capture.side_effect = self.capture_function
        self.operation = self.operation.model_copy(
            update={"operation_id": operation_id, "idempotency_key": operation_id}
        )
        self.fixture.before, self.fixture.candidate = before, candidate
        self.fixture.route = compile_testing_route(
            product=first.product,
            context=first.context,
            host=previous.plan.route.host,
            old_backend=previous.plan.route.new_backend,
            new_backend=f"http://{namespace}:{inputs.runtime_environment['ODOO_HTTP_PORT']}",
            raw=self.raw,
            public_entrypoints=previous.plan.route.public_entrypoints,
            owned_document=self.owned,
            recorded_template=previous.plan.route.template,
        )
        self.app = self.app.model_copy(update={"plan": inputs.plan})
        self.deployment_history.side_effect = lambda **kw: (
            list(app_fixture.deployments) if kw["target_id"] == candidate.compose_id else []
        )

        def ready(**_: Any) -> TestingCandidatePrepared:
            self.assertTrue(app_fixture.restored)
            return TestingCandidatePrepared(
                plan_sha256=inputs.plan.digest,
                runtime_identity=inputs.plan.runtime_identity,
                private_endpoint=self.fixture.route.new_backend,
                deployment_id="candidate-deployment",
                source_witness="empty:" + inputs.plan.digest,
                integration_isolation_verified=True,
                credentials_verified=True,
                observed_at=datetime.now(UTC),
            )

        def request(**kw: Any) -> Any:
            if kw["path"] == "/api/deployment.allByType" or (
                kw["path"] == "/api/compose.update"
                and kw["payload"]["composeId"] == candidate.compose_id
            ):
                return app_fixture.configure(**kw)
            return self.provider_request(**kw)

        prepare = service.NativeTestingRebuildExecution.prepare

        def interleave_prepare(execution: Any, *args: Any, **kwargs: Any) -> Any:
            result = prepare(execution, *args, **kwargs)
            if refresh_during_prepare:
                refresh = self.refresh_through_http("during-next-preparation")
                self.assertEqual(refresh["refreshed_count"], 1, refresh)
            return result

        with (
            patch.object(service.NativeTestingRebuildExecution, "prepare", new=interleave_prepare),
            patch.object(api, "dokploy_request", side_effect=request),
            patch.object(candidate_application, "candidate_private_readiness", side_effect=ready),
            patch.object(service, "candidate_private_readiness", side_effect=ready),
        ):
            self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(app_fixture.effects, ["configure", "deploy", "restore"])
        self.assertEqual(self.effects, ["switch-new", "stop-old"] * 2)
        current = self.current()
        self.assertEqual(current.binding.domains, before.binding.domains)
        self.assertEqual(self.store.list_secret_records(), secrets)
        publication = published_testing_copy(
            self.store, product=candidate.product, context=candidate.context
        )
        assert publication is not None
        compose = yaml.safe_load(
            render_maintained_testing_copy(
                publication, image_reference=inputs.plan.image_reference, hold_web=True
            )
        )
        web = compose["services"]["web"]
        self.assertFalse(web.get("ports"))
        self.assertTrue(
            any(namespace in network.get("aliases", []) for network in web["networks"].values())
        )
        exercise_published_copy_maintenance(self, self.fixture)

    def test_active_old_deployment_refuses_before_candidate_or_route_effects(self) -> None:
        self.deployment_history.return_value = [
            {"deploymentId": "still-building", "status": "running"}
        ]
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.effects, [])

    def test_candidate_custody_changed_after_private_proof_prevents_cutover(self) -> None:
        def rebound(**_: Any) -> Any:
            candidate = self.fixture.candidate
            reservation = self.store.read_provider_target_record(
                context_name=candidate.namespace, instance_name="testing-candidate"
            )
            self.store.write_provider_target_record(
                reservation.model_copy(update={"target_id": "foreign-target"})
            )
            return self.prepared.model_copy(update={"observed_at": datetime.now(UTC)})

        with patch.object(service, "candidate_private_readiness", side_effect=rebound):
            with self.assertRaises(ValueError):
                self.execute()
        self.assertEqual(self.effects, [])
        self.assertFalse(self.retained["stopped"])

    def test_lost_stop_reply_is_observed_without_a_second_stop(self) -> None:
        self.fail_after_stop = True
        with self.assertRaises(TimeoutError):
            self.execute()
        result = self.execute()
        self.assertEqual(result.deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def test_existing_queue_selects_native_execution_and_preserves_pending_observations(
        self,
    ) -> None:
        import logging
        from contextlib import nullcontext
        from control_plane.contracts.durable_operation_authorization import (
            DurableOperationAuthorization,
        )
        from control_plane.workflows import odoo_stable_operation_worker as worker
        from tests.support.durable_operations import (
            durable_operation_authorization_payload,
            durable_operation_policy_record,
        )

        authorization = durable_operation_authorization_payload(
            action="odoo_target_replacement_apply.execute",
            managed_rule_id="native-testing-fixture",
            product=self.operation.product,
            context=self.operation.context,
        )
        operation = self.operation.model_copy(
            update={
                "authorization": DurableOperationAuthorization.model_validate(authorization),
                "status": "running",
                "phase": "running",
                "lease_owner": "worker",
                "started_at": self.operation.created_at,
            }
        )
        success = OdooStableTargetReplacementApplyResult(
            product=self.operation.product,
            context=self.operation.context,
            instance="testing",
            strategy="replace-and-cutover",
            deployment_record_id=self.app.plan.runtime_identity.deployment_record_id,
            deploy_status="pass",
            post_deploy_status="pass",
            health_status="pass",
        )
        for response in (
            success,
            service.NativeTestingRebuildPending("fixture observation pending"),
            RuntimeError("Fixture execution lock lost after dispatch"),
            KeyError("Fixture malformed frame after dispatch"),
        ):
            with (
                self.subTest(pending=isinstance(response, Exception)),
                patch.object(
                    worker,
                    "read_active_authz_policy_record",
                    return_value=durable_operation_policy_record(authorization),
                ),
                patch.object(
                    worker,
                    "execute_native_testing_rebuild",
                    side_effect=response if isinstance(response, Exception) else None,
                    return_value=response,
                ) as execute,
                patch.object(worker, "execute_odoo_stable_target_replacement_apply") as legacy,
                patch.object(
                    worker,
                    "native_rebuild_effects_started",
                    return_value=isinstance(response, Exception),
                ),
                patch.object(
                    self.store,
                    "complete_odoo_stable_target_replacement_operation_record",
                    return_value=True,
                ) as complete,
                self.assertLogs(level=logging.WARNING)
                if isinstance(response, Exception)
                else nullcontext(),
            ):
                self.assertTrue(
                    worker._execute_target_replacement_with_checkpoint(
                        record_store=self.store,
                        control_plane_root_path=self.root,
                        operation=operation,
                        lease_owner="worker",
                        lease_seconds=300,
                        heartbeat_seconds=60,
                    )
                )
            execute.assert_called_once()
            legacy.assert_not_called()
            terminal = complete.call_args.kwargs["record"]
            self.assertEqual(
                terminal.status, "pending" if isinstance(response, Exception) else "pass"
            )
            self.assertFalse(terminal.finished_at if isinstance(response, Exception) else False)
            if isinstance(response, service.NativeTestingRebuildPending):
                self.assertEqual(terminal.error_message, str(response))

    def test_lost_execution_lock_after_route_dispatch_keeps_original_observation(self) -> None:
        from contextlib import contextmanager

        original_lock = self.store.testing_replacement_execution_lock

        @contextmanager
        def lock(operation_id: str) -> Any:
            with original_lock(operation_id) as check:

                def guarded() -> None:
                    assert check is not None
                    check()
                    if self.owned is not None:
                        raise RuntimeError("Fixture execution lock connection lost")

                yield guarded

        with (
            patch.object(self.store, "testing_replacement_execution_lock", side_effect=lock),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        self.assertEqual(self.effects, ["switch-new"])
        self.assertTrue(service.native_rebuild_effects_started(self.store, self.operation))
        self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def test_automatic_old_deploys_are_disabled_only_after_the_new_copy_is_selected(self) -> None:
        self.old_provider["autoDeploy"] = True

        def update(**kwargs: Any) -> dict[str, Any]:
            if kwargs["path"] == "/api/compose.stop":
                return self.provider_request(**kwargs)
            self.assertEqual(kwargs["payload"]["composeId"], self.intent.old.target_id)
            self.assertEqual(self.current().target_id.target_id, self.fixture.candidate.compose_id)
            self.assertFalse(self.retained["stopped"])
            self.old_provider["autoDeploy"] = kwargs["payload"]["autoDeploy"]
            self.effects.append("disable-old-auto-deploy")
            return dict(self.old_provider)

        with patch.object(api, "dokploy_request", side_effect=update):
            self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "disable-old-auto-deploy", "stop-old"])

    def test_rollback_restores_original_auto_deploy_after_excluding_unstarted_stop(self) -> None:
        self.intent = self.intent.model_copy(update={"old_auto_deploy": True})
        self.capture.return_value = self.intent
        self.old_provider["autoDeploy"] = True
        changes: list[bool] = []

        def update(**kwargs: Any) -> dict[str, Any]:
            self.assertEqual(kwargs["path"], "/api/compose.update")
            enabled = kwargs["payload"]["autoDeploy"]
            self.old_provider["autoDeploy"] = enabled
            changes.append(enabled)
            return dict(self.old_provider)

        effect = service.NativeTestingRebuildExecution.run_effect

        def before_stop(executor: Any, phase: str, *args: Any, **kwargs: Any) -> Any:
            if phase == "retire-old":
                raise TimeoutError("fixture ended before old stop dispatch")
            return effect(executor, phase, *args, **kwargs)

        with patch.object(api, "dokploy_request", side_effect=update):
            with (
                patch.object(service.NativeTestingRebuildExecution, "run_effect", before_stop),
                self.assertRaises(TimeoutError),
            ):
                self.execute()
            self.assertEqual(changes, [False])
            self.fail_public = True
            self.assertEqual(self.execute().deploy_status, "fail")
        self.assertEqual(changes, [False, True])
        self.assertTrue(self.old_provider["autoDeploy"])
        self.assertEqual(self.current(), self.fixture.before)
        self.assertEqual(self.effects, ["switch-new", "switch-old"])
        self.assertFalse(self.retained["stopped"])

    def test_unknown_usage_does_not_require_a_down_lane_to_rebuild(self) -> None:
        with patch.object(
            service, "read_odoo_testing_usage", return_value=OdooTestingUsage("unknown")
        ):
            self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.current().target_id.target_id, self.fixture.candidate.compose_id)
        self.assertTrue(self.retained["stopped"])

    def test_verified_down_checkpoint_allows_a_historical_login(self) -> None:
        from tests.support.down_lanes import record_lane_health
        from tests.support.disposable_lanes import record_safety

        record_lane_health(
            self.store, self.operation.product, self.operation.context, datetime.now(UTC)
        )
        self.intent = self.intent.model_copy(
            update={"profile": self.store.read_product_profile_record(self.operation.product)}
        )
        self.capture.return_value = self.intent
        with patch.object(
            service,
            "read_odoo_testing_usage",
            return_value=OdooTestingUsage("in_use", True, True, "complete"),
        ):
            adapter = service.NativeTestingRebuildAdapter(
                root=self.root,
                store=self.store,
                operation=self.operation,
                intent=self.intent,
                authorize=self.authorize,
            )
            executor = service.NativeTestingRebuildExecution(
                adapter, Mock(spec=["assert_current"]), Mock()
            )
            executor.host, executor.token = "fixture-host", "fixture-token"
            for recorded_use in ("unused", "in_use"):
                with self.subTest(recorded_use=recorded_use):
                    record_safety(
                        self.store,
                        self.operation.product,
                        self.operation.context,
                        "testing",
                        usage=recorded_use,
                    )
                    executor.guard()
        self.assertEqual(self.effects, [])

    def test_hold_or_positive_use_refuses_before_any_candidate_or_route_effect(self) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold

        target = self.fixture.before.target
        held = target.model_copy(
            update={
                "policies": target.policies.model_copy(
                    update={
                        "staff_testing_hold": DokployTargetStaffTestingHold(
                            purpose="staff_testing",
                            reason="fixture staff session",
                            recorded_by="fixture",
                            recorded_at=datetime.now(UTC).isoformat(),
                        )
                    }
                )
            }
        )
        self.store.write_dokploy_target_record(held)
        with self.assertRaises(ValueError):
            self.execute()
        self.assertEqual(self.effects, [])
        self.store.write_dokploy_target_record(target)
        with (
            patch.object(
                service,
                "read_odoo_testing_usage",
                return_value=OdooTestingUsage("in_use", True, True, "complete"),
            ),
            self.assertRaises(ValueError),
        ):
            self.execute()
        self.assertEqual(self.effects, [])

    def test_expired_native_worker_keeps_its_original_key_and_resumes_observation(self) -> None:
        from datetime import timedelta

        now = datetime.now(UTC)
        expired = self.operation.model_copy(
            update={
                "status": "running",
                "phase": "running",
                "started_at": self.operation.created_at,
                "lease_owner": "ended-worker",
                "lease_expires_at": (now - timedelta(seconds=1)).isoformat(),
                "attempt": 8,
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(expired)
        recovered_ids = self.store.recover_expired_odoo_stable_target_replacement_operation_records(
            now=now.isoformat(), safe_phases=("created", "plan"), max_attempts=3
        )
        self.assertIn(expired.operation_id, recovered_ids)
        recovered = self.store.read_odoo_stable_target_replacement_operation_record(
            expired.operation_id
        )
        self.assertEqual(recovered.status, "pending")
        self.assertEqual(recovered.request, expired.request)
        self.assertEqual(recovered.idempotency_key, expired.idempotency_key)
        self.assertEqual(recovered.attempt, expired.attempt)
        self.assertFalse(recovered.finished_at)
        self.assertEqual(self.effects, [])

    def test_resume_after_application_start_does_not_require_empty_storage_again(self) -> None:
        with patch.object(
            service,
            "prepare_candidate_storage",
            side_effect=AssertionError("Populated candidate data cannot be qualified as empty"),
        ):
            self.assertEqual(self.execute().deploy_status, "pass")

    def test_provider_permission_failure_is_not_retried_or_relabelled_as_pending(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied

        refusal = api.DokployRequestFailed(
            method="GET", path="/api/compose.one", status_code=403, detail="fixture refusal"
        )
        with (
            patch.object(api, "fetch_dokploy_target_payload", side_effect=refusal) as request,
            self.assertRaises(NativeSourceAuthorizationDenied),
        ):
            self.execute()
        request.assert_called_once()
        self.assertEqual(self.effects, [])

    def test_post_effect_denial_keeps_the_lane_fenced_without_another_worker_attempt(self) -> None:
        from datetime import timedelta
        from control_plane.contracts.durable_operation_authorization import (
            DurableOperationAuthorization,
        )
        from control_plane.contracts.promotion_record import HealthcheckEvidence
        from control_plane.dokploy.native_copy_stream import NativeSourceAuthorizationDenied
        from control_plane.workflows import odoo_stable_operation_worker as worker
        from tests.support.durable_operations import (
            durable_operation_authorization_payload,
            durable_operation_policy_record,
        )

        def health(base_url: str, path: str) -> HealthcheckEvidence:
            if self.owned is not None:
                return HealthcheckEvidence(status="pending", urls=(base_url,))
            return self.health(base_url, path)

        with (
            patch.object(service, "read_public_testing_health", side_effect=health),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        authorization = durable_operation_authorization_payload(
            action="odoo_target_replacement_apply.execute",
            managed_rule_id="native-testing-fixture",
            product=self.operation.product,
            context=self.operation.context,
        )
        now = datetime.now(UTC)
        operation = self.operation.model_copy(
            update={
                "schema_version": 2,
                "authorization": DurableOperationAuthorization.model_validate(authorization),
                "status": "running",
                "phase": "running",
                "attempt": 1,
                "lease_owner": "worker",
                "started_at": now.isoformat(),
                "lease_expires_at": (now + timedelta(minutes=5)).isoformat(),
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(operation)
        denial = NativeSourceAuthorizationDenied("Fixture provider authorization denial")

        def denied_after_retry(**kwargs: Any) -> Any:
            authorize = kwargs["authorize"]

            def checkpoint(phase: str) -> None:
                authorize(phase)
                if phase == "testing_rebuild_checkpoint":
                    original = self.store.read_idempotency_record(
                        scope=REBUILD_SCOPE,
                        route_path=REBUILD_ROUTE,
                        idempotency_key=operation.operation_id,
                    )
                    assert original is not None
                    self.assertGreater(original.attempt, 1)
                    self.assertFalse(original.provider_effect_started_at)
                    raise denial

            return service.execute_native_testing_rebuild(**{**kwargs, "authorize": checkpoint})

        with (
            patch.object(
                worker,
                "read_active_authz_policy_record",
                return_value=durable_operation_policy_record(authorization),
            ),
            patch.object(
                worker, "execute_native_testing_rebuild", side_effect=denied_after_retry
            ) as execute,
        ):
            self.assertTrue(
                worker._execute_target_replacement_with_checkpoint(
                    record_store=self.store,
                    control_plane_root_path=self.root,
                    operation=operation,
                    lease_owner="worker",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )
            )
        held = self.store.read_odoo_stable_target_replacement_operation_record(
            operation.operation_id
        )
        self.assertEqual(held.status, "reconciliation_required")
        self.assertEqual(held.error_code, denial.code)
        self.assertFalse(held.finished_at)
        self.assertIsNone(
            self.store.claim_next_odoo_stable_target_replacement_operation_record(
                lease_owner="another-worker",
                claimed_at=now.isoformat(),
                lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
            )
        )
        execute.assert_called_once()
        self.assertEqual(self.effects, ["switch-new"])

    def test_terminal_preparation_receipt_replays_without_new_provider_or_runtime_reads(
        self,
    ) -> None:
        from control_plane.workflows.testing_rebuild import TestingRebuildPreparationFailure

        apply = service.NativeTestingRebuildAdapter.apply

        def lost(adapter: service.NativeTestingRebuildAdapter, key: str, lease: Any) -> Any:
            apply(adapter, key, lease)
            raise TimeoutError("fixture lost logical completion")

        with (
            patch.object(
                service.NativeTestingRebuildExecution,
                "prepare",
                side_effect=TestingRebuildPreparationFailure(
                    copy_id=self.fixture.candidate.namespace,
                    evidence_id="fixture-terminal-candidate-job",
                ),
            ),
            patch.object(
                service.NativeTestingRebuildAdapter, "apply", autospec=True, side_effect=lost
            ),
            self.assertRaises(TimeoutError),
        ):
            self.execute()
        with (
            patch.object(
                service,
                "candidate_runtime_inputs",
                side_effect=AssertionError(
                    "Historical receipts do not recompile mutable runtime inputs"
                ),
            ),
            patch.object(
                source,
                "read_dokploy_config",
                side_effect=AssertionError(
                    "Historical receipts do not resolve provider credentials"
                ),
            ),
        ):
            self.assertEqual(self.execute().deploy_status, "fail")
        self.assertEqual(self.effects, [])

    def test_terminal_private_probe_failure_is_bounded_and_keeps_old_copy_serving(self) -> None:
        from control_plane.dokploy import DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable
        from control_plane.testing_candidate_application import (
            CandidatePrivateReadinessFailed,
            CandidateObservationUnavailable,
        )
        from control_plane.testing_rebuild_storage import RecordTestingRebuildStore

        original_readiness = service.NativeTestingRebuildExecution.readiness
        plans: list[Any] = []

        def readiness(executor: Any, plan: Any) -> Any:
            plans.append(plan)
            return original_readiness(executor, plan)

        with (
            patch.object(
                service,
                "candidate_private_readiness",
                side_effect=CandidatePrivateReadinessFailed("fixture credential mismatch"),
            ),
            patch.object(service.NativeTestingRebuildExecution, "readiness", readiness),
        ):
            with self.assertRaises(service.NativeTestingRebuildPending):
                self.execute()
            checkpoint = RecordTestingRebuildStore(self.store)
            record = checkpoint.load(plans[0])
            self.assertIsNotNone(record.private_readiness_failed_at)
            with self.assertRaises(ValueError):
                checkpoint.save(record.model_copy(update={"private_readiness_failed_at": None}))
            timeout = DEFAULT_DOKPLOY_DEPLOY_TIMEOUT_SECONDS
            future = datetime.now(UTC) + timedelta(seconds=timeout + 1)
            with patch.object(service, "datetime") as clock:
                clock.now.return_value = future
                with (
                    patch.object(
                        service,
                        "candidate_private_readiness",
                        side_effect=NativeSourceUnavailable("fixture lost connection"),
                    ),
                    patch.object(service, "candidate_terminal_custody_evidence") as terminal,
                    self.assertRaises(service.NativeTestingRebuildPending),
                ):
                    self.execute()
                terminal.assert_not_called()
                with (
                    patch.object(
                        service,
                        "candidate_terminal_custody_evidence",
                        side_effect=CandidateObservationUnavailable(
                            "fixture original job still running"
                        ),
                    ),
                    self.assertRaises(service.NativeTestingRebuildPending),
                ):
                    self.execute()
                with patch.object(
                    service,
                    "candidate_terminal_custody_evidence",
                    return_value="original-restore:done",
                ):
                    self.assertEqual(
                        self.execute().error_code, "candidate_private_readiness_failed"
                    )
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current(), self.fixture.before)
        self.assertFalse(self.retained["stopped"])

    def test_route_writer_excluded_before_dispatch_fails_with_old_copy_serving(self) -> None:
        from control_plane.dokploy.testing_route_transport import TestingRouteWriteNotStarted

        def excluded(*args: Any, dispatch_guard: Any, **kwargs: Any) -> None:
            def send() -> None:
                raise TestingRouteWriteNotStarted("fixture route precondition failed")

            dispatch_guard(send)

        with patch.object(DokployTestingRouteTransport, "publish", side_effect=excluded) as writer:
            result = self.execute()
            self.assertEqual(result.error_code, "candidate_route_not_applied")
            self.assertEqual(self.execute(), result)
            self.assertEqual(writer.call_count, 1)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current(), self.fixture.before)
        self.assertFalse(self.retained["stopped"])

    def test_completed_route_exclusion_resumes_without_private_readiness(self) -> None:
        from control_plane.dokploy.testing_route_transport import TestingRouteWriteNotStarted
        from control_plane.testing_rebuild_storage import RecordTestingRebuildStore

        save = RecordTestingRebuildStore.save

        def interrupted(store: Any, record: Any) -> None:
            if record.step == "failed":
                raise TimeoutError("fixture ended after original writer exclusion")
            save(store, record)

        def excluded(*args: Any, dispatch_guard: Any, **kwargs: Any) -> None:
            def send() -> None:
                raise TestingRouteWriteNotStarted("fixture route precondition failed")

            dispatch_guard(send)

        with (
            patch.object(DokployTestingRouteTransport, "publish", side_effect=excluded),
            patch.object(RecordTestingRebuildStore, "save", interrupted),
            self.assertRaises(TimeoutError),
        ):
            self.execute()
        with (
            patch.object(
                service.NativeTestingRebuildExecution,
                "readiness",
                side_effect=AssertionError(
                    "A completed route exclusion does not depend on candidate health"
                ),
            ),
            patch.object(
                DokployTestingRouteTransport,
                "publish",
                side_effect=AssertionError(
                    "A completed route exclusion never starts another forward writer"
                ),
            ),
        ):
            self.assertEqual(self.execute().error_code, "candidate_route_not_applied")
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current(), self.fixture.before)

    def test_inverse_prewrite_failure_retries_only_its_original_excluded_writer(self) -> None:
        from control_plane.dokploy.testing_route_transport import TestingRouteWriteNotStarted

        self.fail_public = True
        inverse_attempts = 0

        def publish(plan: Any, backend: str, *, dispatch_guard: Any, **kwargs: Any) -> str:
            nonlocal inverse_attempts
            if backend == plan.old_backend:
                inverse_attempts += 1
                if inverse_attempts == 1:

                    def send() -> None:
                        raise TestingRouteWriteNotStarted("fixture old alias unavailable")

                    dispatch_guard(send)
            return self.switch(plan, backend, dispatch_guard=dispatch_guard, **kwargs)

        with patch.object(DokployTestingRouteTransport, "publish", side_effect=publish):
            with self.assertRaises(service.NativeTestingRebuildPending):
                self.execute()
            self.assertEqual(self.effects, ["switch-new"])
            self.assertEqual(self.execute().deploy_status, "fail")
        self.assertEqual(inverse_attempts, 2)
        self.assertEqual(self.effects, ["switch-new", "switch-old"])
        self.assertEqual(self.current(), self.fixture.before)
        self.assertFalse(self.retained["stopped"])

    def test_late_route_writer_cannot_be_overridden_while_its_original_outcome_is_unknown(
        self,
    ) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        pending: list[Any] = []

        def delayed(
            plan: Any,
            backend: str,
            *,
            expected_backend: str,
            dispatch_guard: Any,
            desired_runtime_identity: RuntimeIdentity | None = None,
            execution: Any = None,
            execution_key: str = "",
            writer_attempt: Any = None,
        ) -> None:
            def send() -> None:
                pending.append(
                    lambda: self.switch(
                        plan,
                        backend,
                        expected_backend=expected_backend,
                        dispatch_guard=lambda write: write(),
                        desired_runtime_identity=desired_runtime_identity,
                    )
                )
                raise NativeSourceUnavailable("fixture proxy writer has not returned")

            dispatch_guard(send)

        with (
            patch.object(DokployTestingRouteTransport, "publish", side_effect=delayed),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current(), self.fixture.before)
        with (
            patch.object(
                DokployTestingRouteTransport,
                "publish",
                side_effect=AssertionError("A still-pending writer cannot be replaced or inverted"),
            ),
            patch.object(
                service.NativeTestingRebuildExecution,
                "readiness",
                side_effect=ValueError("fixture candidate failed while writer was pending"),
            ),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        self.assertEqual(self.effects, [])
        self.assertFalse(self.retained["stopped"])

        self.assertEqual(len(pending), 1)
        pending.pop()()
        self.fail_public = True
        self.assertEqual(self.execute().deploy_status, "fail")
        self.assertEqual(self.effects, ["switch-new", "switch-old"])
        self.assertEqual(self.current(), self.fixture.before)
        self.assertFalse(self.retained["stopped"])

    def test_interrupted_generation_preparation_can_continue_the_forward_switch(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        fence = self.route_fence_ensure.return_value
        self.route_fence_ensure.side_effect = [
            NativeSourceUnavailable("fixture unknown initializer before route dispatch"),
            fence,
        ]
        with self.assertRaises(service.NativeTestingRebuildPending):
            self.execute()
        self.assertEqual(self.effects, [])
        self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def real_route_fixture(self) -> Any:
        from tests import test_testing_route_transport as shell_tests
        from control_plane.testing_route_effects import TestingRouteFenceController

        shell = shell_tests.TestingRouteTransportTests()
        shell.setUp(initialize_fence=False)
        self.addCleanup(shell.doCleanups)
        shell.plan = self.fixture.route
        shell.raw = deepcopy(self.raw)
        shell.identity, shell.new_identity = self.original_identity, self.app.plan.runtime_identity
        shell.route_file = shell.root / (shell.plan.route_id + ".yml")
        for key, backend, project, identity in (
            ("old", shell.plan.old_backend, self.intent.project, shell.identity),
            (
                "new",
                shell.plan.new_backend,
                str(urlsplit(shell.plan.new_backend).hostname),
                shell.new_identity,
            ),
        ):
            name = str(urlsplit(backend).hostname)
            container = shell.containers[key]
            container["Name"] = "/" + name
            container["Config"]["Labels"]["com.docker.compose.project"] = project
            container["Config"]["Env"] = [
                "PLATFORM_INSTANCE=testing",
                "LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + identity.model_dump_json(),
            ]
            container["NetworkSettings"]["Networks"]["dokploy-network"]["Aliases"] = [name]
        self.enterContext(patch.multiple(DokployTestingRouteTransport, **self.real_route_transport))
        self.enterContext(
            patch.object(TestingRouteFenceController, "ensure", self.real_route_fence_ensure)
        )

        def health(base_url: str, path: str) -> HealthcheckEvidence:
            self.owned = (
                json.loads(shell.route_file.read_text()) if shell.route_file.exists() else None
            )
            return self.health(base_url, path)

        self.enterContext(patch.object(service, "read_public_testing_health", side_effect=health))
        return shell

    def test_worker_real_route_lost_reply_adopts_the_original_physical_writer(self) -> None:
        shell = self.real_route_fixture()
        shell.before_publish = lambda: setattr(shell, "lose_writer_reply", True)
        with self.assertRaises(service.NativeTestingRebuildPending):
            self.execute()
        self.assertFalse(self.retained["stopped"])
        self.assertEqual(self.current(), self.fixture.before)
        original = shell.route_file.read_bytes()
        sends = shell.remote_modes.count("publish_v2")
        shell.before_publish = None
        shell.lose_writer_reply = False
        self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(shell.remote_modes.count("publish_v2"), sends)
        self.assertEqual(shell.route_file.read_bytes(), original)
        self.assertTrue(self.retained["stopped"])

    def test_worker_real_route_retries_reader_contention_without_failing_the_candidate(
        self,
    ) -> None:
        import fcntl
        import os

        shell = self.real_route_fixture()
        held: list[int] = []

        def reader() -> None:
            descriptor = os.open(shell.root / ("." + shell.route_file.name + ".lock"), os.O_RDONLY)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held.append(descriptor)

        shell.before_publish = reader
        try:
            with self.assertRaises(service.NativeTestingRebuildPending):
                self.execute()
        finally:
            for descriptor in held:
                os.close(descriptor)
        self.assertTrue(held)
        self.assertFalse(self.retained["stopped"])
        self.assertFalse(shell.route_file.exists())
        shell.before_publish = None
        self.assertEqual(self.execute().deploy_status, "pass")
        self.assertTrue(self.retained["stopped"])

    def test_sealed_non_applied_writer_finishes_without_candidate_readiness(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        def delayed(*args: Any, dispatch_guard: Any, **kwargs: Any) -> None:
            def send() -> None:
                raise NativeSourceUnavailable("fixture lost original writer response")

            dispatch_guard(send)

        with (
            patch.object(DokployTestingRouteTransport, "publish", side_effect=delayed),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        with (
            patch.object(DokployTestingRouteTransport, "settle_attempt", return_value="excluded"),
            patch.object(
                DokployTestingRouteTransport,
                "publish",
                side_effect=AssertionError("A sealed original cannot dispatch again"),
            ),
            patch.object(
                service.NativeTestingRebuildExecution,
                "readiness",
                side_effect=AssertionError("Exclusion is independent of candidate readiness"),
            ),
        ):
            result = self.execute()
        self.assertEqual(result.error_code, "candidate_route_not_applied")
        self.assertEqual(self.current(), self.fixture.before)
        self.assertEqual(self.effects, [])
        self.assertFalse(self.retained["stopped"])

    def test_legacy_writer_never_gets_a_guessed_execution_exclusion(self) -> None:
        from control_plane.contracts.testing_rebuild import REBUILD_SCOPE, REBUILD_ROUTE
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        def delayed(*args: Any, dispatch_guard: Any, **kwargs: Any) -> None:
            def send() -> None:
                raise NativeSourceUnavailable("fixture lost legacy writer response")

            dispatch_guard(send)

        with patch.object(DokployTestingRouteTransport, "publish", side_effect=delayed):
            with self.assertRaises(service.NativeTestingRebuildPending):
                self.execute()
        phase = "route:" + self.fixture.candidate.namespace
        prior = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=self.operation.operation_id + ":" + phase,
        )
        assert prior is not None
        legacy = prior.model_copy(update={"provider_effect_phase": phase})
        self.store._write_row(self.store._idempotency_row(legacy))
        read_intent = self.store.read_original_mutation_intent
        with (
            patch.object(
                self.store,
                "read_original_mutation_intent",
                side_effect=lambda record: (
                    {} if record.record_id == legacy.record_id else read_intent(record)
                ),
            ),
            patch.object(
                DokployTestingRouteTransport,
                "settle_attempt",
                side_effect=AssertionError("Legacy custody is not guessed"),
            ),
            patch.object(
                DokployTestingRouteTransport,
                "publish",
                side_effect=AssertionError("An unknown legacy dispatch is not repeated"),
            ),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        self.assertEqual(self.effects, [])
        self.assertEqual(self.current(), self.fixture.before)

    def test_excluded_inverse_attempt_uses_a_new_key_after_proven_exclusion(self) -> None:
        from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable

        self.fail_public = True
        keys: list[str] = []

        def interrupted(
            plan: Any, backend: str, *, dispatch_guard: Any, execution_key: str, **kwargs: Any
        ) -> str:
            if backend == plan.old_backend:
                keys.append(execution_key)
                if len(keys) == 1:

                    def send() -> None:
                        raise NativeSourceUnavailable("fixture lost inverse response")

                    dispatch_guard(send)
            return self.switch(
                plan, backend, dispatch_guard=dispatch_guard, execution_key=execution_key, **kwargs
            )

        def settle(attempt: Any, *, dispatch_guard: Any) -> str:
            execution, key = attempt.execution, attempt.key
            if execution.backend == self.fixture.route.old_backend:
                self.assertEqual(key, keys[-1])
                dispatch_guard(lambda: None)
                return "excluded"
            return self.settle_execution(execution, key, dispatch_guard=dispatch_guard)

        with patch.object(DokployTestingRouteTransport, "publish", side_effect=interrupted):
            with self.assertRaises(service.NativeTestingRebuildPending):
                self.execute()
            with patch.object(DokployTestingRouteTransport, "settle_attempt", side_effect=settle):
                self.assertEqual(self.execute().deploy_status, "fail")
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])
        self.assertEqual(self.effects, ["switch-new", "switch-old"])
        self.assertEqual(self.current(), self.fixture.before)
        self.assertFalse(self.retained["stopped"])

    def test_unknown_public_health_keeps_observing_the_same_candidate_without_retirement(
        self,
    ) -> None:
        unknown = self.fixture.health.model_copy(
            update={"verified": False, "status": "pending", "observed_runtime_identity": None}
        )
        with (
            patch.object(service, "read_public_testing_health", return_value=unknown),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        self.assertEqual(self.effects, ["switch-new"])
        self.assertFalse(self.retained["stopped"])
        self.assertEqual(self.current(), self.fixture.before)
        self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def test_failed_health_after_a_crash_before_retirement_excludes_stop_then_restores_old(
        self,
    ) -> None:
        with (
            patch.object(
                service.NativeTestingRebuildExecution,
                "retire_old",
                side_effect=TimeoutError("fixture ended before old stop"),
            ),
            self.assertRaises(TimeoutError),
        ):
            self.execute()
        self.fail_public = True
        self.assertEqual(self.execute().deploy_status, "fail")
        self.assertEqual(self.effects, ["switch-new", "switch-old"])
        self.assertFalse(self.retained["stopped"])
        self.assertEqual(self.current(), self.fixture.before)

    def test_lost_exclusion_reply_cannot_restart_stop_when_new_health_recovers(self) -> None:
        with (
            patch.object(
                service.NativeTestingRebuildExecution,
                "retire_old",
                side_effect=TimeoutError("fixture ended before old stop"),
            ),
            self.assertRaises(TimeoutError),
        ):
            self.execute()
        self.fail_public = True
        observe = service.NativeTestingRebuildExecution.retirement_exclusion

        def lost(executor: service.NativeTestingRebuildExecution, plan: Any) -> Any:
            proof = observe(executor, plan)
            if proof is not None:
                self.fail_public = False
                raise TimeoutError("fixture lost exclusion reply")
            return proof

        with (
            patch.object(
                service.NativeTestingRebuildExecution,
                "retirement_exclusion",
                autospec=True,
                side_effect=lost,
            ),
            self.assertRaises(TimeoutError),
        ):
            self.execute()
        self.old_private_healthy = False
        with self.assertRaises(service.NativeTestingRebuildPending):
            self.execute()
        self.old_private_healthy = True
        self.assertEqual(self.execute().deploy_status, "fail")
        self.assertEqual(self.effects, ["switch-new", "switch-old"])
        self.assertFalse(self.retained["stopped"])

    def test_unknown_dispatched_stop_is_not_excluded_or_repeated_when_health_recovers(self) -> None:
        self.fail_after_stop = True
        with self.assertRaises(TimeoutError):
            self.execute()
        self.fail_public = True
        with self.assertRaises(service.NativeTestingRebuildPending):
            self.execute()
        self.assertEqual(self.effects, ["switch-new", "stop-old"])
        self.fail_public = False
        self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def test_provider_reply_does_not_retire_until_every_old_service_is_observed_stopped(
        self,
    ) -> None:
        def partial(**kwargs: Any) -> dict[str, Any]:
            result = self.provider_request(**kwargs)
            self.retained["stopped"] = False  # web stopped, another old service is still running
            return result

        with (
            patch.object(api, "dokploy_request", side_effect=partial),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        self.assertTrue(self.retained["web_stopped"])
        with self.assertRaises(service.NativeTestingRebuildPending):
            self.execute()
        self.assertEqual(self.effects, ["switch-new", "stop-old"])
        self.retained["stopped"] = True
        self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])


class NativeHistoricalUseTests(unittest.TestCase):
    database_url = ""

    def setUp(self) -> None:
        from tests import test_testing_candidate_application as application_tests
        from tests.support.down_lanes import record_lane_health

        execute = application_tests.CandidateApplicationTests.execution

        def configured(fixture: Any, source: Any = "empty") -> None:
            # Monitoring is part of the original candidate authority, configured
            # before allocation/application, not changed under a prepared copy.
            record_lane_health(
                fixture.store, fixture.fixture.product, fixture.fixture.context, datetime.now(UTC)
            )
            fixture.fixture.before = fixture.fixture.authority()
            execute(fixture, source)

        self.fixture = NativeTestingWorkerTests()
        self.fixture.database_url = self.database_url
        with patch.object(application_tests.CandidateApplicationTests, "execution", configured):
            self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def complete_historical_use_rebuild(
        self, recorded: Literal["unused", "in_use"], fresh: Literal["unused", "in_use"]
    ) -> None:
        from tests.support.disposable_lanes import record_safety

        fixture = self.fixture
        record_safety(
            fixture.store,
            fixture.operation.product,
            fixture.operation.context,
            "testing",
            usage=recorded,
        )
        with patch.object(service, "read_odoo_testing_usage", return_value=OdooTestingUsage(fresh)):
            self.assertEqual(fixture.execute().deploy_status, "pass")
        self.assertEqual(
            fixture.current().target_id.target_id, fixture.fixture.candidate.compose_id
        )
        self.assertEqual(fixture.effects, ["switch-new", "stop-old"])

    def test_saved_historical_use_completes_after_its_rebuild_changes_monitor_identity(
        self,
    ) -> None:
        self.complete_historical_use_rebuild("in_use", "unused")

    def test_fresh_historical_login_completes_after_its_rebuild_restores_health(self) -> None:
        self.complete_historical_use_rebuild("unused", "in_use")
