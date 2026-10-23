"""Explicit fresh authorization resumes only the original fenced rebuild."""

from datetime import UTC, datetime
from contextlib import nullcontext
from dataclasses import replace
import unittest
from unittest.mock import patch
from httpx2 import Response
from sqlalchemy import event
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.sql.dml import Insert
from typing import Any

from control_plane import testing_rebuild_service as service
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_bootstrap import OdooStableBootstrapRequest
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.testing_rebuild import REBUILD_SCOPE, REBUILD_ROUTE
from control_plane.storage.postgres import LaunchplaneIdempotencyRow
from control_plane.durable_operation_authorization import (
    capture_durable_operation_authorization,
    read_active_authz_policy_record,
)
from control_plane.drivers.native_routes import _descriptor_driver_route_authz_action
from control_plane.odoo_target_replacement_apply_http import ODOO_TARGET_REPLACEMENT_APPLY_ROUTE
from tests import test_testing_rebuild_http as http_tests
from tests import test_testing_rebuild_worker as worker_tests
from tests import test_odoo_prod_promotion_operation as administrator_tests
from control_plane.http_routes import testing_rebuild as routes
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.provider_schedule import ScheduleMaterializationTooLarge
from control_plane.durable_operation_authorization import DurableOperationAuthorizationDeniedError


class TestingRebuildResumeTests(unittest.IsolatedAsyncioTestCase):
    database_url = ""

    def setUp(self) -> None:
        self.http = http_tests.TestingRebuildHttpFixture()
        with patch.object(worker_tests.NativeTestingWorkerTests, "database_url", self.database_url):
            self.http.setUp()
        self.addCleanup(self.http.doCleanups)
        self.worker, self.store = self.http.fixture, self.http.store
        identity = self.http.session.identity
        operation = self.worker.operation
        authorization = capture_durable_operation_authorization(
            identity=identity,
            action=_descriptor_driver_route_authz_action(ODOO_TARGET_REPLACEMENT_APPLY_ROUTE),
            product=operation.product,
            context=operation.context,
            instances=(operation.instance,),
            policy_record=read_active_authz_policy_record(self.store),
            authorized_at=datetime.now(UTC).isoformat(),
        )

        def uncertain_health(base_url: str, path: str) -> HealthcheckEvidence:
            if self.worker.owned is not None:
                return HealthcheckEvidence(status="pending", urls=(base_url,))
            return self.worker.health(base_url, path)

        with (
            patch.object(service, "read_public_testing_health", side_effect=uncertain_health),
            self.assertRaises(service.NativeTestingRebuildPending),
        ):
            self.worker.execute()
        self.held = operation.model_copy(
            update={
                "schema_version": 2,
                "authorization": authorization,
                "status": "reconciliation_required",
                "phase": "running",
                "attempt": operation.attempt + 1,
                "updated_at": datetime.now(UTC).isoformat(),
                "error_code": "authorization_denied",
                "error_message": "Fixture provider identity refused continuation",
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(self.held)
        self.path = self.http.url + "/operations/" + self.held.operation_id + "/resume"
        self.payload = {
            "expected_updated_at": self.held.updated_at,
            "expected_attempt": self.held.attempt,
        }
        self.outer = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE,
            idempotency_key=self.held.operation_id,
        )
        self.child = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=self.held.operation_id
            + ":route:"
            + self.worker.fixture.candidate.namespace,
        )

    async def resume(self, *, csrf: bool = True) -> Response:
        return await self.http.call(
            "POST", self.payload, key=self.held.idempotency_key, path=self.path, csrf=csrf
        )

    def assert_original_fences(self) -> None:
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=REBUILD_SCOPE,
                route_path=REBUILD_ROUTE,
                idempotency_key=self.held.operation_id,
            ),
            self.outer,
        )
        self.assertEqual(
            self.store.read_idempotency_record(
                scope=REBUILD_SCOPE,
                route_path=REBUILD_ROUTE + ":effect",
                idempotency_key=self.held.operation_id
                + ":route:"
                + self.worker.fixture.candidate.namespace,
            ),
            self.child,
        )
        self.assertEqual(self.worker.effects, ["switch-new"])

    async def test_explicit_resume_keeps_the_same_request_and_provider_fences(self) -> None:
        before = self.store.list_odoo_stable_target_replacement_operation_records()
        self.http.latest.side_effect = AssertionError("Resume cannot select another build")
        response = await self.resume()
        self.assertTrue(response.is_success, response.text)
        current = self.store.read_odoo_stable_target_replacement_operation_record(
            self.held.operation_id
        )
        self.assertEqual(current.status, "pending")
        self.assertEqual(current.request, self.held.request)
        self.assertEqual(current.idempotency_key, self.held.idempotency_key)
        self.assertEqual(current.attempt, self.held.attempt)
        self.assertEqual(
            len(self.store.list_odoo_stable_target_replacement_operation_records()), len(before)
        )
        self.assert_original_fences()

    async def test_director_operator_resumes_its_original_standing_rebuild_without_a_rule(
        self,
    ) -> None:
        from control_plane.http_app import create_launchplane_fastapi_app
        from control_plane.service_auth import (
            BearerIdentityConfig,
            configured_local_operator_identity,
        )
        from control_plane.testing_rebuild_authorization import (
            capture_director_testing_rebuild_authorization,
        )
        from tests.http_app_test_support import _RejectingVerifier
        from tests.support.http import request

        config = BearerIdentityConfig(
            local_operator_token="fixture-operator",
            local_operator_subject="fixture-director",
            local_operator_token_label="fixture-token",
        )
        identity = configured_local_operator_identity(config)
        assert identity is not None
        authorization = capture_director_testing_rebuild_authorization(
            identity=identity,
            request=self.held.request,
            context=self.held.context,
            store=self.store,
            authorized_at=self.held.created_at,
        )
        self.held = self.held.model_copy(update={"authorization": authorization})
        self.store.write_odoo_stable_target_replacement_operation_record(self.held)
        app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=self.http.policy,
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.worker.root,
            bearer_identity_config=config,
        )
        response = await request(
            app,
            "POST",
            self.path,
            headers={
                "Authorization": "Bearer fixture-operator",
                "Idempotency-Key": self.held.idempotency_key,
            },
            payload=self.payload,
        )
        self.assertEqual(response.status_code, 200, response.text)
        resumed = self.store.read_odoo_stable_target_replacement_operation_record(
            self.held.operation_id
        )
        self.assertEqual(resumed.status, "pending")
        assert resumed.authorization is not None and self.held.authorization is not None
        self.assertEqual(resumed.authorization.caller, self.held.authorization.caller)
        self.assertEqual(resumed.authorization.grant, self.held.authorization.grant)
        self.assertEqual(resumed.request, self.held.request)
        self.assert_original_fences()

    async def test_repeating_the_same_resume_does_not_requeue_again(self) -> None:
        first = await self.resume()
        self.assertTrue(first.is_success, first.text)
        from datetime import timedelta

        now = datetime.now(UTC)
        after = self.store.claim_next_odoo_stable_target_replacement_operation_record(
            lease_owner="already-running",
            claimed_at=now.isoformat(),
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
        assert after is not None
        repeated = await self.resume()
        self.assertTrue(repeated.is_success, repeated.text)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(self.held.operation_id),
            after,
        )
        self.assert_original_fences()

    async def test_missing_browser_evidence_cannot_resume(self) -> None:
        response = await self.resume(csrf=False)
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(self.held.operation_id),
            self.held,
        )
        self.assert_original_fences()

    async def test_stale_held_revision_cannot_resume(self) -> None:
        self.payload["expected_attempt"] = self.held.attempt + 1
        response = await self.resume()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(self.held.operation_id),
            self.held,
        )
        self.assert_original_fences()

    async def test_another_active_lane_owner_prevents_resume(self) -> None:
        successor = OdooStableBootstrapOperationRecord(
            operation_id=self.held.operation_id + "-bootstrap",
            product=self.held.product,
            context=self.held.context,
            instance=self.held.instance,
            idempotency_key="bootstrap-fixture",
            request_fingerprint="bootstrap-fixture",
            request=OdooStableBootstrapRequest(
                product=self.held.product,
                context=self.held.context,
                instance=self.held.instance,
                confirmation=f"bootstrap {self.held.context} {self.held.instance}",
            ),
            created_at=self.held.updated_at,
            updated_at=self.held.updated_at,
        )
        self.store.write_odoo_stable_bootstrap_operation_record(successor)
        response = await self.resume()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(self.held.operation_id),
            self.held,
        )
        self.assertEqual(
            self.store.read_odoo_stable_bootstrap_operation_record(successor.operation_id),
            successor,
        )
        self.assert_original_fences()

    async def test_another_signed_in_caller_cannot_resume_the_original(self) -> None:
        original = self.http.session.identity
        self.http.session = self.http.sessions.issue(
            replace(original, login="another-administrator", github_id=original.github_id + 1)
        )
        response = await self.resume()
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(self.held.operation_id),
            self.held,
        )
        self.assert_original_fences()

    async def test_current_authorization_is_checked_beyond_the_existing_browser_session(
        self,
    ) -> None:
        active = read_active_authz_policy_record(self.store)
        revoked = administrator_tests._policy_record(
            active.policy.model_copy(update={"github_humans": ()}), revision=active.revision + 1
        )
        with patch.object(routes, "read_active_authz_policy_record", return_value=revoked):
            response = await self.resume()
        self.assertEqual(response.status_code, 403, response.text)
        self.assertTrue(response.json()["trace_id"])
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(self.held.operation_id),
            self.held,
        )
        self.assert_original_fences()

    async def test_resume_captures_current_policy_instead_of_copying_old_authorization(
        self,
    ) -> None:
        active = read_active_authz_policy_record(self.store)
        refreshed = administrator_tests._policy_record(active.policy, revision=active.revision + 1)
        with patch.object(routes, "read_active_authz_policy_record", return_value=refreshed):
            response = await self.resume()
        self.assertTrue(response.is_success, response.text)
        current = self.store.read_odoo_stable_target_replacement_operation_record(
            self.held.operation_id
        )
        assert current.authorization is not None
        self.assertEqual(current.authorization.policy_record_id, refreshed.record_id)
        self.assertEqual(current.authorization.policy_revision, refreshed.revision)
        self.assert_original_fences()

    async def test_running_or_terminal_operations_cannot_be_reopened(self) -> None:
        for status in ("running", "fail"):
            with self.subTest(status=status):
                current = self.held.model_copy(
                    update={
                        "status": status,
                        "phase": "running" if status == "running" else "failed",
                        "finished_at": self.held.updated_at if status == "fail" else "",
                    }
                )
                self.store.write_odoo_stable_target_replacement_operation_record(current)
                response = await self.resume()
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    self.store.read_odoo_stable_target_replacement_operation_record(
                        self.held.operation_id
                    ),
                    current,
                )
                self.assert_original_fences()

    async def test_running_refusal_does_not_lock_the_live_provider_receipt(self) -> None:
        current = self.held.model_copy(update={"status": "running", "phase": "running"})
        self.store.write_odoo_stable_target_replacement_operation_record(current)
        statement = self.store._idempotency_statement

        def preserve_live_receipt(**kwargs: Any) -> Any:
            if kwargs["scope"] == REBUILD_SCOPE and kwargs.get("for_update"):
                self.fail("A refused running operation must not lock its live provider receipt")
            return statement(**kwargs)

        with patch.object(self.store, "_idempotency_statement", side_effect=preserve_live_receipt):
            response = await self.resume()
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(current.operation_id),
            current,
        )
        self.assert_original_fences()

    async def test_resume_receipt_failure_rolls_back_the_queue_transition(self) -> None:
        from control_plane.testing_rebuild_resume import RESUME_SCOPE

        def failed_receipt(
            connection: Connection,
            statement: Any,
            multiparams: Any,
            params: Any,
            execution_options: Any,
        ) -> None:
            if (
                isinstance(statement, Insert)
                and statement.table.name == LaunchplaneIdempotencyRow.__tablename__
                and any(
                    isinstance(values, dict) and values.get("scope") == RESUME_SCOPE
                    for values in (*multiparams, params)
                )
            ):
                raise RuntimeError("Fixture resume receipt write failed")

        event.listen(Engine, "before_execute", failed_receipt)
        try:
            with self.assertRaises(RuntimeError):
                await self.resume()
        finally:
            event.remove(Engine, "before_execute", failed_receipt)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(self.held.operation_id),
            self.held,
        )
        self.assert_original_fences()
        response = await self.resume()
        self.assertTrue(response.is_success, response.text)

    async def test_concurrent_resume_calls_share_one_queue_transition(self) -> None:
        from concurrent.futures import ThreadPoolExecutor

        assert self.held.authorization is not None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(
                    self.store.resume_native_testing_rebuild,
                    operation=self.held,
                    authorization=self.held.authorization,
                    expected_updated_at=self.held.updated_at,
                    expected_attempt=self.held.attempt,
                    trace_id=f"resume-fixture-{index}",
                    now=datetime.now(UTC).isoformat(),
                )
                for index in range(2)
            ]
            results = [future.result() for future in futures]
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0].status, "pending")
        self.assertEqual(results[0].request, self.held.request)
        self.assert_original_fences()

    async def test_original_worker_continues_without_another_route_dispatch(self) -> None:
        await self.resume_and_complete_original_worker()

    async def test_worker_denial_holds_original_fences_before_explicit_resume(self) -> None:
        from datetime import timedelta
        from control_plane.workflows import odoo_stable_operation_worker as worker
        from control_plane.durable_operation_authorization import (
            DurableOperationAuthorizationDeniedError,
            DurableOperationAuthorizationGuard,
        )

        pending = self.held.model_copy(
            update={
                "status": "pending",
                "phase": self.worker.operation.phase,
                "error_code": "",
                "error_message": "",
            }
        )
        self.store.write_odoo_stable_target_replacement_operation_record(pending)
        now = datetime.now(UTC)
        claimed = self.store.claim_next_odoo_stable_target_replacement_operation_record(
            lease_owner="denied-worker",
            claimed_at=now.isoformat(),
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
        assert claimed is not None
        denial = DurableOperationAuthorizationDeniedError(
            code="fixture_checkpoint_denied", message="Fixture provider checkpoint refused"
        )
        with patch.object(
            DurableOperationAuthorizationGuard, "recheck_provider_effect", side_effect=denial
        ):
            self.assertTrue(
                worker._execute_target_replacement_with_checkpoint(
                    record_store=self.store,
                    control_plane_root_path=self.worker.root,
                    operation=claimed,
                    lease_owner=claimed.lease_owner,
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )
            )
        self.held = self.store.read_odoo_stable_target_replacement_operation_record(
            claimed.operation_id
        )
        self.assertEqual(self.held.status, "reconciliation_required")
        self.assertEqual(self.held.error_code, denial.code)
        self.assertEqual(self.held.authorization, claimed.authorization)
        self.assertEqual(self.held.phase, claimed.phase)
        self.assertFalse(self.held.finished_at)
        self.assertFalse(self.held.lease_owner)
        self.assertFalse(self.held.lease_expires_at)
        self.assertFalse(self.held.heartbeat_at)
        self.payload = {
            "expected_updated_at": self.held.updated_at,
            "expected_attempt": self.held.attempt,
        }
        self.assert_original_fences()
        await self.resume_and_complete_original_worker()

    async def test_worker_size_refusal_holds_custody_until_explicit_original_resume(self) -> None:
        from datetime import timedelta
        from control_plane.contracts.provider_schedule import ScheduleMaterializationTooLarge
        from control_plane.workflows import odoo_stable_operation_worker as worker

        pending = self.held.model_copy(
            update={"status": "pending", "phase": "created", "error_code": "", "error_message": ""}
        )
        self.store.write_odoo_stable_target_replacement_operation_record(pending)
        now = datetime.now(UTC)
        claimed = self.store.claim_next_odoo_stable_target_replacement_operation_record(
            lease_owner="size-refused-worker",
            claimed_at=now.isoformat(),
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
        assert claimed is not None
        original = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE, route_path=REBUILD_ROUTE, idempotency_key=claimed.operation_id
        )
        assert original is not None
        allocation = self.store.read_testing_candidate(self.worker.fixture.candidate.namespace)
        assert allocation is not None
        refusal = ScheduleMaterializationTooLarge()
        with patch.object(service.NativeTestingRebuildExecution, "run", side_effect=refusal) as run:
            self.assertTrue(
                worker._execute_target_replacement_with_checkpoint(
                    record_store=self.store,
                    control_plane_root_path=self.worker.root,
                    operation=claimed,
                    lease_owner=claimed.lease_owner,
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )
            )
        run.assert_called_once()
        self.held = self.store.read_odoo_stable_target_replacement_operation_record(
            claimed.operation_id
        )
        self.assertEqual(self.held.status, "reconciliation_required")
        self.assertEqual(self.held.error_code, refusal.code)
        self.assertEqual(self.held.phase, claimed.phase)
        for value in (
            self.held.finished_at,
            self.held.lease_owner,
            self.held.lease_expires_at,
            self.held.heartbeat_at,
        ):
            self.assertFalse(value)
        retained = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE, route_path=REBUILD_ROUTE, idempotency_key=claimed.operation_id
        )
        assert retained is not None
        self.assertEqual(retained.state, "reconcile_required")
        self.assertEqual(retained.provider_effect_phase, original.provider_effect_phase)
        self.assertEqual(retained.provider_effect_started_at, original.provider_effect_started_at)
        self.assertEqual(self.store.read_testing_candidate(allocation.namespace), allocation)
        self.assertIsNone(
            self.store.claim_next_odoo_stable_target_replacement_operation_record(
                lease_owner="automatic-retry",
                claimed_at=now.isoformat(),
                lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
            )
        )
        self.payload = {
            "expected_updated_at": self.held.updated_at,
            "expected_attempt": self.held.attempt,
        }
        self.assertEqual(self.worker.effects, ["switch-new"])
        await self.resume_and_complete_original_worker()
        final = self.store.read_testing_candidate(allocation.namespace)
        assert final is not None
        self.assertEqual(final.compose_id, allocation.compose_id)

    async def resume_and_complete_original_worker(self) -> None:
        from datetime import timedelta
        from control_plane.workflows import odoo_stable_operation_worker as worker

        response = await self.resume()
        self.assertTrue(response.is_success, response.text)
        now = datetime.now(UTC)
        claimed = self.store.claim_next_odoo_stable_target_replacement_operation_record(
            lease_owner="resumed-worker",
            claimed_at=now.isoformat(),
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
        assert claimed is not None
        self.assertEqual(claimed.operation_id, self.held.operation_id)
        self.assertTrue(
            worker._execute_target_replacement_with_checkpoint(
                record_store=self.store,
                control_plane_root_path=self.worker.root,
                operation=claimed,
                lease_owner=claimed.lease_owner,
                lease_seconds=300,
                heartbeat_seconds=60,
            )
        )
        completed = self.store.read_odoo_stable_target_replacement_operation_record(
            self.held.operation_id
        )
        self.assertEqual(completed.status, "pass")
        self.assertEqual(completed.request, self.held.request)
        self.assertEqual(self.worker.effects, ["switch-new", "stop-old"])


class TestingRebuildBeforeEffectsTests(
    http_tests.TestingRebuildHttpFixture, unittest.IsolatedAsyncioTestCase
):
    def fail_before_effects(
        self, error: Exception, *, legacy: bool = False
    ) -> OdooStableTargetReplacementOperationRecord:
        from datetime import timedelta
        from control_plane.workflows import odoo_stable_operation_worker as worker

        original = self.fixture.operation
        authorization = capture_durable_operation_authorization(
            identity=self.session.identity,
            action=_descriptor_driver_route_authz_action(ODOO_TARGET_REPLACEMENT_APPLY_ROUTE),
            product=original.product,
            context=original.context,
            instances=(original.instance,),
            policy_record=read_active_authz_policy_record(self.store),
            authorized_at=datetime.now(UTC).isoformat(),
        )
        original = original.model_copy(
            update={
                "schema_version": 2,
                "operation_id": original.operation_id + "-before-effects",
                "authorization": authorization,
            }
        )
        self.fixture.operation = original
        self.fixture.intent = self.fixture.intent.model_copy(
            update={"operation_id": original.operation_id}
        )
        self.fixture.capture.return_value = self.fixture.intent
        self.store.write_odoo_stable_target_replacement_operation_record(original)
        now = datetime.now(UTC)
        claimed = self.store.claim_next_odoo_stable_target_replacement_operation_record(
            lease_owner="before-effects-worker",
            claimed_at=now.isoformat(),
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
        assert claimed is not None
        with (
            patch.object(service, "prepare_candidate_storage", side_effect=error),
            patch.object(
                self.store,
                "native_testing_rebuild_requires_reconciliation",
                return_value=False,
            )
            if legacy
            else nullcontext(),
        ):
            self.assertTrue(
                worker._execute_target_replacement_with_checkpoint(
                    record_store=self.store,
                    control_plane_root_path=self.fixture.root,
                    operation=claimed,
                    lease_owner=claimed.lease_owner,
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )
            )
        current = self.store.read_odoo_stable_target_replacement_operation_record(
            original.operation_id
        )
        self.assertFalse(service.native_rebuild_effects_started(self.store, current))
        self.assertEqual(self.fixture.effects, [])
        receipt = self.outer(current)
        assert receipt is not None
        self.assertEqual(receipt.state, "reconcile_required")
        self.assertFalse(receipt.provider_effect_started_at)
        return current

    def outer(
        self, operation: OdooStableTargetReplacementOperationRecord
    ) -> LaunchplaneIdempotencyRecord | None:
        return self.store.read_idempotency_record(
            scope=REBUILD_SCOPE, route_path=REBUILD_ROUTE, idempotency_key=operation.operation_id
        )

    async def resume_original(
        self, operation: OdooStableTargetReplacementOperationRecord
    ) -> Response:
        return await self.call(
            "POST",
            {"expected_updated_at": operation.updated_at, "expected_attempt": operation.attempt},
            key=operation.idempotency_key,
            path=self.url + "/operations/" + operation.operation_id + "/resume",
        )

    async def assert_pre_effect_failure_is_held(self, error: Exception) -> None:
        operation = self.fail_before_effects(error)
        self.assertEqual(operation.status, "reconciliation_required")
        self.assertFalse(
            any(
                (
                    operation.finished_at,
                    operation.lease_owner,
                    operation.lease_expires_at,
                    operation.heartbeat_at,
                )
            )
        )
        receipt = self.outer(operation)
        response = await self.resume_original(operation)
        self.assertTrue(response.is_success, response.text)
        resumed = self.store.read_odoo_stable_target_replacement_operation_record(
            operation.operation_id
        )
        self.assertEqual(resumed.status, "pending")
        self.assertEqual(resumed.request, operation.request)
        self.assertEqual(self.outer(operation), receipt)

    async def test_worker_unexpected_failure_keeps_original_reservation_resumable(self) -> None:
        await self.assert_pre_effect_failure_is_held(
            RuntimeError("Fixture failure before allocation")
        )

    async def test_worker_denial_before_effects_keeps_original_reservation_resumable(self) -> None:
        await self.assert_pre_effect_failure_is_held(
            DurableOperationAuthorizationDeniedError(
                code="fixture_denied", message="Fixture refusal before allocation"
            )
        )

    async def test_worker_size_refusal_before_effects_keeps_original_reservation_resumable(
        self,
    ) -> None:
        await self.assert_pre_effect_failure_is_held(ScheduleMaterializationTooLarge())

    async def test_legacy_failed_original_resumes_without_releasing_or_replacing_receipts(
        self,
    ) -> None:
        operation = self.fail_before_effects(
            RuntimeError("Fixture historical failure"), legacy=True
        )
        self.assertEqual(operation.status, "fail")
        receipt = self.outer(operation)
        assert receipt is not None
        with self.assertRaises(ValueError):
            self.store.write_odoo_stable_target_replacement_operation_record(
                operation.model_copy(update={"status": "pending", "phase": "created"})
            )
        response = await self.resume_original(operation)
        self.assertTrue(response.is_success, response.text)
        resumed = self.store.read_odoo_stable_target_replacement_operation_record(
            operation.operation_id
        )
        self.assertEqual(resumed.status, "pending")
        self.assertEqual(resumed.request, operation.request)
        self.assertEqual(resumed.idempotency_key, operation.idempotency_key)
        self.assertEqual(resumed.attempt, operation.attempt)
        self.assertFalse(
            any(
                (
                    resumed.finished_at,
                    resumed.lease_owner,
                    resumed.lease_expires_at,
                    resumed.heartbeat_at,
                )
            )
        )
        self.assertEqual(self.outer(operation), receipt)
        repeated = await self.resume_original(operation)
        self.assertTrue(repeated.is_success, repeated.text)
        self.assertEqual(
            self.store.read_odoo_stable_target_replacement_operation_record(operation.operation_id),
            resumed,
        )
        from datetime import timedelta
        from control_plane.contracts.odoo_stable_target_replacement import (
            OdooStableTargetReplacementApplyResult,
        )
        from control_plane.provider_operations import ProviderMutationOutcome
        from control_plane.workflows import odoo_stable_operation_worker as worker

        result = OdooStableTargetReplacementApplyResult(
            product=operation.product,
            context=operation.context,
            instance=operation.instance,
            strategy=operation.request.strategy,
            deployment_record_id=self.fixture.app.plan.runtime_identity.deployment_record_id,
            deploy_status="pass",
            post_deploy_status="pass",
            health_status="pass",
        )
        now = datetime.now(UTC)
        claimed = self.store.claim_next_odoo_stable_target_replacement_operation_record(
            lease_owner="resumed-before-effects-worker",
            claimed_at=now.isoformat(),
            lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
        )
        assert claimed is not None
        self.assertEqual(claimed.operation_id, operation.operation_id)
        intent = self.store.read_original_mutation_intent(receipt)
        with patch.object(
            service.NativeTestingRebuildAdapter,
            "apply",
            return_value=ProviderMutationOutcome(
                200, {"result": result.model_dump(mode="json")}, provider_effect_performed=False
            ),
        ) as apply:
            self.assertTrue(
                worker._execute_target_replacement_with_checkpoint(
                    record_store=self.store,
                    control_plane_root_path=self.fixture.root,
                    operation=claimed,
                    lease_owner=claimed.lease_owner,
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )
            )
            completed = self.store.read_odoo_stable_target_replacement_operation_record(
                operation.operation_id
            )
            self.assertEqual(completed.status, "pass")
            self.assertEqual(completed.request, operation.request)
            self.assertEqual(completed.idempotency_key, operation.idempotency_key)
            self.assertEqual(self.store.read_original_mutation_intent(receipt), intent)
            settled = self.outer(operation)
            assert settled is not None
            self.assertEqual(settled.state, "completed")
            self.assertEqual(settled.reconciliation_key, receipt.reconciliation_key)
            repeated = await self.resume_original(operation)
            self.assertTrue(repeated.is_success, repeated.text)
            self.assertEqual(
                self.store.read_odoo_stable_target_replacement_operation_record(
                    operation.operation_id
                ),
                completed,
            )
            apply.assert_called_once()

    async def test_legacy_failed_original_requires_the_matching_held_receipt(self) -> None:
        operation = self.fail_before_effects(
            RuntimeError("Fixture historical failure"), legacy=True
        )
        receipt = self.outer(operation)
        assert receipt is not None
        completed = receipt.model_copy(
            update={
                "state": "completed",
                "response_status_code": 200,
                "response_trace_id": "completed",
                "recorded_at": receipt.updated_at,
            }
        )
        for replacement in (
            None,
            completed,
            receipt.model_copy(update={"state": "running"}),
            receipt.model_copy(update={"request_fingerprint": "another-request"}),
        ):
            with self.subTest(receipt=replacement):
                with self.store._session_factory() as session:
                    row = session.get(LaunchplaneIdempotencyRow, receipt.record_id)
                    assert row is not None
                    if replacement is None:
                        session.delete(row)
                    else:
                        self.store._sync_idempotency_row(row, replacement)
                    session.commit()
                response = await self.resume_original(operation)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(
                    self.store.read_odoo_stable_target_replacement_operation_record(
                        operation.operation_id
                    ),
                    operation,
                )
                with self.store._session_factory() as session:
                    session.merge(self.store._idempotency_row(receipt))
                    session.commit()
