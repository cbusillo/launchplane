"""The durable worker joins preparation, unchanged routing, adoption and retirement."""

from copy import deepcopy
from datetime import UTC, datetime
import json
from urllib.parse import urlsplit
from typing import Any
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
from control_plane.contracts.testing_rebuild import TestingCopy
from control_plane.dokploy.testing_route import route_document
from control_plane.dokploy.testing_route_transport import DokployTestingRouteTransport
from control_plane.dokploy import post_deploy, api
from control_plane.odoo_testing_usage import OdooTestingUsage
from control_plane.testing_publication_storage import read_snapshot
from tests import test_testing_publication as publication_tests
from tests import test_testing_route as route_tests


class NativeTestingWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = publication_tests.TestingPublicationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store, self.root = self.fixture.store, self.fixture.fixture.root
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
            "stopped": False,
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
                resource_binding=self.retained["resource_binding"],
                runtime_identity=self.original_identity,
            ),
            retained=self.retained,
            public_entrypoints=self.fixture.route.public_entrypoints,
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
        self.authorize = Mock()
        self.old_provider: dict[str, Any] = {
            "composeId": self.intent.old.target_id,
            "name": self.fixture.before.target.target_name,
            "appName": self.intent.project,
            "serverId": self.intent.server_id,
            "autoDeploy": False,
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

        self.capture = self.enterContext(
            patch.object(service, "capture_rebuild_intent", return_value=self.intent)
        )
        self.enterContext(
            patch.object(service, "retained_copy_operation", side_effect=self.physical)
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

    def physical(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs["payload"]["mode"] == "stop":

            def stop() -> None:
                self.assertIsNotNone(self.owned)
                self.assertEqual(
                    self.current().target_id.target_id, self.fixture.candidate.compose_id
                )
                self.assertFalse(self.fail_public)
                self.effects.append("stop-old")
                self.retained["stopped"] = True

            kwargs["dispatch_guard"](stop)
            if self.fail_after_stop:
                self.fail_after_stop = False
                raise TimeoutError("fixture lost retirement reply")
        return dict(self.retained)

    def route_read(self) -> dict[str, Any]:
        return {
            "raw": deepcopy(self.raw),
            "owned_file": json.dumps(self.owned) if self.owned is not None else None,
        }

    def switch(
        self, plan: Any, backend: str, *, expected_backend: str, dispatch_guard: Any
    ) -> None:
        def publish() -> None:
            self.assertEqual(self.current(), self.fixture.before)
            self.assertFalse(self.retained["stopped"])
            self.effects.append("switch-new" if backend == plan.new_backend else "switch-old")
            self.owned = route_document(plan, backend)
            for section in ("routers", "services", "middlewares"):
                for name, value in self.owned["http"].get(section, {}).items():
                    self.raw[section][name + "@file"] = {**value, "status": "enabled"}

        dispatch_guard(publish)

    def health(self, base_url: str, path: str) -> Any:
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

    def test_retained_credential_peers_come_from_this_lanes_publication_custody(self) -> None:
        self.execute()
        with patch.object(
            api, "fetch_dokploy_target_payload", return_value=dict(self.old_provider)
        ):
            peers = service.retained_testing_projects(
                self.store,
                before=self.current(),
                product=self.operation.product,
                context=self.operation.context,
                host="provider.invalid",
                token="fixture",
                server_id=self.intent.server_id,
            )
        self.assertEqual(peers, (self.intent.project,))
        foreign = self.fixture.before.target_id.model_copy(
            update={"context": "foreign", "instance": "prod"}
        )
        self.store.write_dokploy_target_id_record(foreign)
        with (
            patch.object(api, "fetch_dokploy_target_payload", return_value=dict(self.old_provider)),
            self.assertRaises(ValueError),
        ):
            service.retained_testing_projects(
                self.store,
                before=self.current(),
                product=self.operation.product,
                context=self.operation.context,
                host="provider.invalid",
                token="fixture",
                server_id=self.intent.server_id,
            )

    def test_automatic_old_deploys_are_disabled_only_after_the_new_copy_is_selected(self) -> None:
        self.old_provider["autoDeploy"] = True

        def update(**kwargs: Any) -> dict[str, Any]:
            self.assertEqual(kwargs["payload"]["composeId"], self.intent.old.target_id)
            self.assertEqual(self.current().target_id.target_id, self.fixture.candidate.compose_id)
            self.assertFalse(self.retained["stopped"])
            self.old_provider["autoDeploy"] = kwargs["payload"]["autoDeploy"]
            self.effects.append("disable-old-auto-deploy")
            return dict(self.old_provider)

        with patch.object(api, "dokploy_request", side_effect=update):
            self.assertEqual(self.execute().deploy_status, "pass")
        self.assertEqual(self.effects, ["switch-new", "disable-old-auto-deploy", "stop-old"])

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
