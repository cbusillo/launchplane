"""Explicit fresh authorization resumes only the original fenced rebuild."""

from datetime import UTC, datetime
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
