"""The durable worker joins preparation, unchanged routing, adoption and retirement."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from urllib.parse import urlsplit
from typing import Any, Literal
import unittest
from unittest.mock import Mock, patch

from control_plane import testing_rebuild_service as service
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementApplyResult,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.testing_rebuild import TestingCopy
from control_plane.contracts.testing_candidate import testing_candidate_namespace
from control_plane.contracts.testing_candidate_application import TestingCandidatePrepared
from control_plane.dokploy.testing_route import route_document, compile_testing_route
from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport
from control_plane.dokploy import post_deploy, api, source
from control_plane import testing_candidate_application as candidate_application
from control_plane.testing_candidate_allocation import candidate_resources
from control_plane.testing_candidate_runtime import candidate_runtime_inputs
from control_plane.testing_serving_copy import (
    published_testing_copy,
    render_maintained_testing_copy,
)
from control_plane.odoo_testing_usage import OdooTestingUsage
from control_plane.testing_publication_storage import read_snapshot
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
        self.enterContext(
            patch.object(DokployTestingRouteTransport, "read", side_effect=self.route_read)
        )
        self.enterContext(
            patch.object(DokployTestingRouteTransport, "publish", side_effect=self.switch)
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
        }

    def switch(
        self,
        plan: Any,
        backend: str,
        *,
        expected_backend: str,
        dispatch_guard: Any,
        desired_runtime_identity: RuntimeIdentity | None = None,
    ) -> None:
        def publish() -> None:
            if backend == plan.new_backend:
                self.assertEqual(self.current(), self.fixture.before)
            self.assertFalse(self.retained["stopped"])
            self.effects.append("switch-new" if backend == plan.new_backend else "switch-old")
            self.owned = route_document(plan, backend)
            for section in ("routers", "services", "middlewares"):
                for name, value in self.owned["http"].get(section, {}).items():
                    self.raw[section][name + "@file"] = {**value, "status": "enabled"}

        dispatch_guard(publish)

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
        self.store.write_odoo_stable_target_replacement_operation_record(self.operation)
        self.assertTrue(
            self.store.cancel_pending_odoo_stable_target_replacement_operation_record(
                self.cancellation()
            )
        )
        self.assertEqual(self.effects, [])

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

    def test_hold_after_cutover_does_not_block_inverse_or_erase_the_hold(self) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold

        self.fail_public = True
        hold = DokployTargetStaffTestingHold(reason="Staff need the testing lane")

        def held(plan: Any, backend: str, **kwargs: Any) -> None:
            self.switch(plan, backend, **kwargs)
            if backend == plan.new_backend:
                target = self.current().target
                self.store.write_dokploy_target_record(
                    target.model_copy(
                        update={
                            "policies": target.policies.model_copy(
                                update={"staff_testing_hold": hold}
                            )
                        }
                    )
                )

        with (
            patch.object(DokployTestingRouteTransport, "publish", side_effect=held),
        ):
            self.assertEqual(self.execute().deploy_status, "fail")
        self.assertEqual(self.effects, ["switch-new", "switch-old"])
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
        def edited(plan: Any, backend: str, **kwargs: Any) -> None:
            self.switch(plan, backend, **kwargs)
            if backend == plan.new_backend:
                self.change_unrelated_profile()

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
        with self.assertRaises(ValueError) as caught:
            self.execute()
        self.assertNotIsInstance(caught.exception, service.NativeTestingRebuildPending)
        self.assertFalse(service.native_rebuild_effects_started(self.store, self.operation))
        self.assertEqual(self.effects, [])

    def test_new_hold_after_healthy_cutover_records_reality_and_preserves_old_copy(self) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold

        hold = DokployTargetStaffTestingHold(reason="Staff hold after cutover")

        def held(plan: Any, backend: str, **kwargs: Any) -> None:
            self.switch(plan, backend, **kwargs)
            target = self.current().target
            self.store.write_dokploy_target_record(
                target.model_copy(
                    update={
                        "policies": target.policies.model_copy(update={"staff_testing_hold": hold})
                    }
                )
            )

        with (
            patch.object(DokployTestingRouteTransport, "publish", side_effect=held),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.execute()
        self.assertEqual(self.current().target_id.target_id, self.fixture.candidate.compose_id)
        self.assertEqual(self.current().target.policies.staff_testing_hold, hold)
        self.assertEqual(self.effects, ["switch-new"])
        self.assertFalse(self.retained["stopped"])
        target = self.current().target
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "policies": target.policies.model_copy(update={"staff_testing_hold": None}),
                    "updated_at": datetime.now(UTC).isoformat(),
                }
            )
        )
        self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "stop-old"])

    def test_hold_after_interrupted_cutover_still_allows_original_inverse_on_resume(self) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
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
        target = self.current().target
        hold = DokployTargetStaffTestingHold(reason="Staff hold during the interrupted cutover")
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={"policies": target.policies.model_copy(update={"staff_testing_hold": hold})}
            )
        )
        self.fail_public = True
        self.assertEqual(self.execute().deploy_status, "fail")
        self.assertEqual(self.effects, ["switch-new", "switch-old"])
        self.assertEqual(self.current().target.policies.staff_testing_hold, hold)
        self.assertFalse(self.retained["stopped"])

    def test_second_rebuild_uses_published_copy_and_preserves_later_deployment_route(self) -> None:
        import yaml

        self.assertEqual(self.execute().deploy_status, "pass")
        first = self.fixture.candidate
        previous = published_testing_copy(self.store, product=first.product, context=first.context)
        assert previous is not None
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
            new_backend=f"http://{namespace}:{previous.plan.profile.runtime_port}",
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

        with (
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
        with (
            patch.object(
                worker,
                "read_active_authz_policy_record",
                return_value=durable_operation_policy_record(authorization),
            ),
            patch.object(worker, "execute_native_testing_rebuild", side_effect=denial) as execute,
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

        def publish(plan: Any, backend: str, *, dispatch_guard: Any, **kwargs: Any) -> None:
            nonlocal inverse_attempts
            if backend == plan.old_backend:
                inverse_attempts += 1
                if inverse_attempts == 1:

                    def send() -> None:
                        raise TestingRouteWriteNotStarted("fixture old alias unavailable")

                    dispatch_guard(send)
            self.switch(plan, backend, dispatch_guard=dispatch_guard, **kwargs)

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
