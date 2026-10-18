"""Persist actual emitted fence calls and recover without replaying a nonce."""

import unittest
from unittest.mock import Mock

from control_plane.contracts.testing_rebuild import REBUILD_SCOPE
from control_plane.dokploy.native_copy_stream import (
    NativeSourceAuthorizationDenied,
    NativeSourceUnavailable,
)
from control_plane.dokploy.testing_route_execution import (
    TestingRouteExecutionCustody,
    TestingRouteInitializer,
    TestingRouteLockIntent,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_route_effects import (
    ROUTE_INITIALIZER_RECEIPT,
    ROUTE_LOCK_RECEIPT,
    TestingRouteFenceController,
    TestingRouteEffects,
)
from control_plane.provider_operations import (
    run_durable_provider_operation,
    DurableProviderOperationResult,
)
from control_plane.dokploy.testing_route import observe_testing_route, route_document
from control_plane.contracts.canonical_json import canonical_json_sha256
import json
from tests import test_testing_route_transport as transport_tests


class TestingRouteFenceControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = transport_tests.TestingRouteTransportTests()
        self.fixture.setUp(initialize_fence=False)
        self.addCleanup(self.fixture.doCleanups)
        self.store = PostgresRecordStore(
            database_url="sqlite+pysqlite:///" + str(self.fixture.root / "records.sqlite3")
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        self.execution = self.fixture.client.execution_intent(
            self.fixture.plan, self.fixture.plan.new_backend, operation_id="fixture durable rebuild"
        )
        self.controller = TestingRouteFenceController(
            store=self.store,
            transport=self.fixture.client,
            execution=self.execution,
            guard=Mock(),
            checkpoint=Mock(),
            lease_owner=lambda: "fixture-route-owner",
        )
        self.logical_executions: dict[str, object] = {}

    def run_switch(
        self, *, inverse: bool = False, interrupt: bool = False
    ) -> DurableProviderOperationResult:
        from typing import Any, cast

        backend = self.fixture.plan.old_backend if inverse else self.fixture.plan.new_backend
        expected = self.fixture.plan.new_backend if inverse else self.fixture.plan.old_backend
        phase = "inverse" if inverse else "forward"
        execution = self.logical_executions.setdefault(
            phase,
            self.fixture.client.execution_intent(
                self.fixture.plan,
                backend,
                operation_id=self.execution.operation_id,
            ),
        )
        execution = cast(Any, execution)
        controller = TestingRouteFenceController(
            store=self.store,
            transport=self.fixture.client,
            execution=execution,
            guard=Mock(),
            checkpoint=Mock(),
            lease_owner=lambda: "fixture-route-owner",
        )

        def present() -> bool:
            frame = self.fixture.client.read()
            return (
                observe_testing_route(
                    self.fixture.plan,
                    frame["raw"],
                    json.loads(frame["owned_file"]) if frame["owned_file"] else None,
                )
                == backend
            )

        def publish(attempt: Any, dispatch: Any) -> object:
            self.fixture.lose_writer_reply = interrupt
            try:
                return self.fixture.client.publish(
                    self.fixture.plan,
                    backend,
                    expected_backend=expected,
                    desired_runtime_identity=self.fixture.identity
                    if inverse
                    else self.fixture.new_identity,
                    dispatch_guard=dispatch,
                    execution=execution,
                    execution_key=attempt.key,
                    writer_attempt=attempt,
                )
            finally:
                self.fixture.lose_writer_reply = False

        key = self.execution.operation_id + ":" + phase
        route = TestingRouteEffects(
            controller=controller,
            phase=phase,
            present=present,
            publish=publish,
            recovery_guard=Mock(),
            inverse=inverse,
        )
        from control_plane.contracts.testing_rebuild import REBUILD_ROUTE

        prior = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=key,
        )

        class Adapter:
            def target_key(self) -> str:
                return "fixture-switch:" + key

            def reconciliation_key(self) -> str:
                return canonical_json_sha256({"phase": phase})

            def observe(
                self,
                provider_operation_key: str,
                provider_effect_phase: str,
                reconciliation_key: str,
            ) -> Any:
                assert prior is not None
                return route.observe(provider_effect_phase, prior.attempt)

            def apply(self, provider_operation_key: str, lease: Any) -> Any:
                return route.apply(lease)

        return run_durable_provider_operation(
            store=self.store,
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key=key,
            request_fingerprint=canonical_json_sha256({"phase": phase}),
            lease_owner="fixture-route-owner",
            response_trace_id=self.execution.operation_id,
            adapter=Adapter(),
            original_intent={"route_execution": execution.model_dump(mode="json")},
        )

    def test_physical_writer_lost_reply_is_adopted_without_republishing(self) -> None:
        with self.assertRaises(NativeSourceUnavailable):
            self.run_switch(interrupt=True)
        content = self.fixture.route_file.read_bytes()
        sends = self.fixture.remote_modes.count("publish_v2")
        result = self.run_switch()
        self.assertEqual(result.status, "adopted")
        self.assertEqual(self.fixture.remote_modes.count("publish_v2"), sends)
        self.assertEqual(self.fixture.route_file.read_bytes(), content)

    def test_inverse_retries_on_a_new_root_and_keeps_original_physical_custody(self) -> None:
        from control_plane.testing_route_effects import ROUTE_WRITER_RECEIPT

        self.fixture.root = self.fixture.root / "dynamic"
        self.fixture.root.mkdir()
        self.fixture.route_file = self.fixture.root / self.fixture.route_file.name
        self.assertEqual(self.run_switch().status, "completed")
        with self.assertRaises(NativeSourceUnavailable):
            self.run_switch(inverse=True, interrupt=True)
        originals = [
            self.store.read_original_mutation_intent(record)
            for record in self.store.list_held_provider_target_reservations()
            if record.route_path == ROUTE_WRITER_RECEIPT
        ]
        self.assertEqual(len(originals), 1)
        original = originals[0]["route_writer"]
        content = self.fixture.route_file.read_bytes()
        self.fixture.root.rename(self.fixture.root.with_name("retired"))
        self.fixture.root.mkdir()
        self.fixture.route_file.write_bytes(content)
        # The previous inverse did write on the retired root. Restore the current
        # applied model to the still-serving candidate before recovering it.
        self.fixture.route_file.write_text(
            json.dumps(route_document(self.fixture.plan, self.fixture.plan.new_backend))
        )
        self.fixture.apply_document()
        result = self.run_switch(inverse=True)
        self.assertEqual(result.status, "completed")
        current = json.loads(self.fixture.route_file.read_text())
        self.assertEqual(current, route_document(self.fixture.plan, self.fixture.plan.old_backend))
        rows = self.store.list_held_provider_target_reservations()
        old = next(record for record in rows if record.route_path == ROUTE_WRITER_RECEIPT)
        self.assertEqual(self.store.read_original_mutation_intent(old)["route_writer"], original)

    def custody(self) -> TestingRouteExecutionCustody:
        return TestingRouteExecutionCustody.model_validate(
            self.fixture.client.read()["execution_custody"]
        )

    def test_repeated_use_and_source_respell_keep_the_original_lock_receipt(self) -> None:
        custody = self.custody()
        fence = self.controller.ensure(custody)
        intent = TestingRouteLockIntent(
            custody=custody,
            server_id=self.execution.server_id,
            route_id=self.execution.route_id,
        )
        receipt = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE, route_path=ROUTE_LOCK_RECEIPT, idempotency_key=intent.key
        )
        assert receipt is not None
        original = self.store.read_original_mutation_intent(receipt)
        sends = self.fixture.effect_count
        self.fixture.containers["proxy"]["Mounts"][0]["Source"] = "/fixture/canonical-dynamic"
        self.assertEqual(self.controller.ensure(self.custody()), fence)
        self.assertEqual(self.fixture.effect_count, sends)
        self.assertEqual(self.store.read_original_mutation_intent(receipt), original)
        self.assertFalse(self.fixture.route_file.exists())

    def test_lost_initializer_reply_is_adopted_without_repeating_its_nonce(self) -> None:
        custody = self.custody()
        self.controller.lock(custody)
        self.fixture.lose_writer_reply = True
        with self.assertRaises(NativeSourceUnavailable):
            self.controller.ensure(custody)
        self.fixture.lose_writer_reply = False
        state = self.fixture.client.read_execution_fence(custody)
        generation = state["fence"]["generation"]
        receipt = self.store.read_idempotency_record(
            scope=REBUILD_SCOPE,
            route_path=ROUTE_INITIALIZER_RECEIPT,
            idempotency_key=generation,
        )
        assert receipt is not None
        original = self.store.read_original_mutation_intent(receipt)
        sends = self.fixture.effect_count
        fence = self.controller.ensure(custody)
        self.assertEqual(fence.generation, generation)
        self.assertEqual(self.fixture.effect_count, sends)
        self.assertEqual(self.store.read_original_mutation_intent(receipt), original)

    def test_proxy_restart_reuses_the_directory_fence_without_mutating_it(self) -> None:
        old_custody = self.custody()
        fence = self.controller.ensure(old_custody)
        sends = self.fixture.effect_count
        self.fixture.process_start.write_text("200")
        self.fixture.containers["proxy"]["State"]["StartedAt"] = "fixture-start-successor"
        current = self.custody()
        self.assertNotEqual(old_custody.physical_sha256, current.physical_sha256)
        self.assertEqual(self.controller.ensure(current), fence)
        self.assertEqual(self.fixture.effect_count, sends)

    def test_fresh_boot_recovers_preserved_fence_with_a_renumbered_device(self) -> None:
        original_custody = self.custody()
        original = self.controller.ensure(original_custody)
        self.fixture.process_boot.write_text("87654321-4321-4321-4321-cba987654321")
        self.fixture.device_offset.write_text("100")
        self.fixture.containers["proxy"]["State"]["StartedAt"] = "fixture-fresh-boot"
        current = self.custody()
        successor = self.controller.ensure(current)
        self.assertNotEqual(successor.lock_identity, original.lock_identity)
        self.assertNotEqual(successor.generation, original.generation)
        self.assertEqual(self.controller.ensure(current), successor)
        self.assertFalse(self.fixture.route_file.exists())

    def test_an_unknown_pre_nonce_call_gets_a_distinct_fencing_attempt(self) -> None:
        custody = self.custody()
        self.controller.lock(custody)
        self.fixture.writer_timeout = True
        with self.assertRaises(NativeSourceUnavailable):
            self.controller.ensure(custody)
        self.fixture.writer_timeout = False
        pending = [
            record
            for record in self.store.list_held_provider_target_reservations()
            if record.route_path == ROUTE_INITIALIZER_RECEIPT
        ]
        self.assertEqual(len(pending), 1)
        original = self.store.read_original_mutation_intent(pending[0])
        initializer = TestingRouteInitializer.model_validate(original["route_initializer"])
        fence = self.controller.ensure(custody)
        self.assertNotEqual(fence.generation, initializer.generation)
        self.assertEqual(fence.lock_identity, initializer.lock_identity)
        self.assertEqual(self.store.read_original_mutation_intent(pending[0]), original)
        self.assertFalse(self.fixture.route_file.exists())

    def test_lost_permanent_lock_is_never_recreated_from_a_completed_receipt(self) -> None:
        custody = self.custody()
        self.controller.ensure(custody)
        name = self.fixture.route_file.name
        (self.fixture.root / ("." + name + ".receipt")).unlink()
        (self.fixture.root / ("." + name + ".lock")).unlink()
        sends = self.fixture.effect_count
        with self.assertRaises(NativeSourceUnavailable):
            self.controller.ensure(custody)
        self.assertEqual(self.fixture.effect_count, sends)
        state = self.fixture.client.read_execution_fence(custody)
        self.assertEqual(state["status"], "missing")

    def test_initializer_denial_propagates_without_another_nonce_or_identity(self) -> None:
        custody = self.custody()
        self.controller.lock(custody)
        sends = self.fixture.effect_count
        self.fixture.deny = True
        with self.assertRaises(NativeSourceAuthorizationDenied):
            self.controller.ensure(custody)
        self.assertEqual(self.fixture.effect_count, sends + 1)
        self.assertFalse(self.fixture.route_file.exists())
