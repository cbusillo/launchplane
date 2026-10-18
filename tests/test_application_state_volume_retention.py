from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.application_state_volume import (
    PLAN_ROUTE,
    ROUTE,
    _normalized,
    state_volume_target_key,
    state_volume_allocation_names,
)
from control_plane.application_state_volume_retention import (
    checkpoint_state_volume_progress,
    read_state_volume_progress,
    retain_state_volume_allocation,
    state_volume_custody_sha256,
    state_volume_plan_is_retained,
    state_volume_progress_operation_key,
)
from control_plane.application_state_volume_errors import StateVolumeRefusal
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    format_launchplane_mutation_timestamp,
    parse_launchplane_mutation_timestamp,
)
from control_plane.dokploy_target_setup_http import DokployTargetSetupEnvelope
from control_plane.http_routes.mutation_support import request_fingerprint
from control_plane.storage.postgres import PostgresRecordStore


class ApplicationStateVolumeRetentionTests(unittest.TestCase):
    def database_url(self) -> str:
        directory = self.enterContext(TemporaryDirectory())
        return f"sqlite+pysqlite:///{Path(directory) / 'retention.sqlite3'}"

    def setUp(self) -> None:
        self.store = PostgresRecordStore(database_url=self.database_url())
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        request = DokployTargetSetupEnvelope(
            operation="create-application",
            context="example",
            instance="testing",
            target_name="Example testing",
            project_name="Example testing",
            environment_name="testing",
            server_id="isolated-server",
            state_volume_path="/var/lib/example",
        )
        plan_id = "a" * 32
        self.intent: dict[str, Any] = {
            "request": _normalized(request),
            "product": "example",
            "binding": "fixture-binding",
            "provider_host": "https://provider.example.invalid",
            "plan_id": plan_id,
            **state_volume_allocation_names(plan_id),
        }
        self.intent["plan_sha256"] = request_fingerprint(self.intent)
        self.scope = "terminal-agent:retention-fixture"
        self.store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id="application-state-plan-" + plan_id,
                scope=self.scope,
                route_path=PLAN_ROUTE,
                idempotency_key=plan_id,
                request_fingerprint=self.intent["plan_sha256"],
                response_status_code=202,
                response_trace_id="plan",
                recorded_at="2026-01-01T00:00:00Z",
                response_payload=self.intent,
            )
        )
        acquired = self.store.reserve_mutation(
            scope=self.scope,
            route_path=ROUTE,
            idempotency_key="original-key",
            request_fingerprint=self.intent["plan_sha256"],
            lease_owner="original-worker",
            reconciliation_key=self.intent["plan_sha256"],
            provider_target_key=state_volume_target_key(request.context),
            original_intent=self.intent,
        ).record
        assert acquired is not None
        checkpointed = self.store.checkpoint_mutation_provider_effect(
            reservation=acquired, effect_phase='{"pending": "/api/project.create"}'
        ).record
        assert checkpointed is not None
        self.reservation = checkpointed
        self.digest = state_volume_custody_sha256(self.store, self.reservation)
        self.checkpoint = Mock()

    def expired(self) -> Any:
        now = format_launchplane_mutation_timestamp(
            parse_launchplane_mutation_timestamp(
                self.reservation.lease_expires_at, field_name="lease_expires_at"
            )
            + timedelta(seconds=1)
        )
        return patch.object(self.store, "_database_mutation_timestamp", return_value=now)

    def retain(self, **overrides: Any) -> dict[str, object]:
        arguments: dict[str, Any] = {
            "reservation": self.reservation,
            "expected_custody_sha256": self.digest,
            "expected_original_intent": self.intent,
            "trace": "retain-trace",
            "checkpoint": self.checkpoint,
        }
        arguments.update(overrides)
        return retain_state_volume_allocation(self.store, **arguments)

    def read(self) -> LaunchplaneIdempotencyRecord:
        row = self.store.read_idempotency_record(
            scope=self.scope, route_path=ROUTE, idempotency_key=self.reservation.idempotency_key
        )
        assert row is not None
        return row

    def test_retention_preserves_custody_fences_worker_and_replays(self) -> None:
        with self.expired():
            result = self.retain()
            self.assertEqual(self.retain(), result)
        retained = self.read()
        self.assertEqual(retained.state, "completed")
        self.assertTrue(
            state_volume_plan_is_retained(self.store, self.scope, self.intent["plan_id"])
        )
        self.assertEqual(retained.provider_effect_phase, self.reservation.provider_effect_phase)
        self.assertEqual(self.store.read_original_mutation_intent(retained), self.intent)
        self.assertEqual(retained.response_payload["result"], result)
        self.assertEqual(result["allocation_state"], "retained_uncertain")
        self.assertFalse(result["applied"])
        self.assertTrue(result["resources_retained"])
        self.assertFalse(result["deployment_performed"])
        self.assertEqual(
            state_volume_progress_operation_key(self.store, self.scope, self.intent["plan_id"]),
            self.reservation.idempotency_key,
        )
        self.assertFalse(self.store.list_held_provider_target_reservations())
        self.assertEqual(
            self.store.renew_mutation_reservation(reservation=self.reservation).status,
            "not_running",
        )
        self.assertEqual(
            self.store.checkpoint_mutation_provider_effect(
                reservation=self.reservation, effect_phase="late_application_create"
            ).status,
            "not_running",
        )
        with self.expired():
            self.assertEqual(
                self.retain(
                    reservation=retained,
                    expected_custody_sha256=state_volume_custody_sha256(self.store, retained),
                ),
                result,
            )
            self.assertEqual(self.retain(reservation=retained), result)
            with self.assertRaises(ValueError):
                self.retain(reservation=retained, expected_custody_sha256="another-review")
        self.assertEqual(
            self.store.reserve_mutation(
                scope=self.scope,
                route_path=ROUTE,
                idempotency_key="fresh-reviewed-key",
                request_fingerprint="fresh-plan-digest",
                lease_owner="fresh-worker",
                reconciliation_key="fresh-plan-digest",
                provider_target_key=self.reservation.provider_target_key,
            ).status,
            "acquired",
        )

    def test_active_lease_and_stale_review_refuse_without_changing_custody(self) -> None:
        with self.assertRaises(StateVolumeRefusal) as active:
            self.retain()
        self.assertEqual(active.exception.code, "application_state_volume_execution_active")
        with self.expired(), self.assertRaises(StateVolumeRefusal) as stale:
            self.retain(expected_custody_sha256="wrong-review")
        self.assertEqual(stale.exception.code, "application_state_volume_custody_changed")
        altered = deepcopy(self.intent)
        altered["binding"] = "changed-binding"
        with self.expired(), self.assertRaises(ValueError):
            self.retain(expected_original_intent=altered)
        self.assertEqual(self.read(), self.reservation)
        self.checkpoint.assert_not_called()

    def test_expired_reconcile_required_allocation_can_be_retained(self) -> None:
        marked = self.store.mark_mutation_reconcile_required(
            reservation=self.reservation,
            reconciliation_key=self.reservation.reconciliation_key,
        ).record
        assert marked is not None
        self.reservation = marked
        self.digest = state_volume_custody_sha256(self.store, marked)
        with self.expired():
            self.retain()
        self.assertEqual(self.read().state, "completed")
        self.checkpoint.assert_called_once_with()

    def test_changed_original_row_refuses_even_when_lease_expired(self) -> None:
        updated = self.store.checkpoint_mutation_provider_effect(
            reservation=self.reservation, effect_phase='{"project": "acknowledged-project"}'
        ).record
        assert updated is not None
        with self.expired(), self.assertRaises(ValueError):
            self.retain()
        self.assertEqual(self.read(), updated)

    def test_authority_checkpoint_failure_rolls_back_retention(self) -> None:
        with self.expired(), self.assertRaises(ValueError):
            self.retain(checkpoint=Mock(side_effect=ValueError("lane authority changed")))
        self.assertEqual(self.read(), self.reservation)

    def save_progress(self, reservation: LaunchplaneIdempotencyRecord | None = None) -> None:
        record = reservation or self.reservation
        checkpoint_state_volume_progress(
            self.store,
            reservation=record,
            plan_id=self.intent["plan_id"],
            phase=record.provider_effect_phase,
        )

    def test_live_progress_persists_and_expired_or_retained_worker_cannot_change_it(self) -> None:
        self.assertEqual(
            state_volume_progress_operation_key(self.store, self.scope, self.intent["plan_id"]), ""
        )
        self.assertEqual(
            read_state_volume_progress(self.store, self.scope, self.intent["plan_id"]), ""
        )
        self.save_progress()
        self.assertEqual(
            read_state_volume_progress(self.store, self.scope, self.intent["plan_id"]),
            self.reservation.provider_effect_phase,
        )
        self.digest = state_volume_custody_sha256(self.store, self.reservation)
        with self.expired():
            with self.assertRaises(ValueError):
                self.save_progress()
            result = self.retain()
            with self.assertRaises(ValueError):
                self.save_progress()
        self.assertEqual(
            result["retained_provider_effect_phase"], self.reservation.provider_effect_phase
        )
        self.assertEqual(
            read_state_volume_progress(self.store, self.scope, self.intent["plan_id"]),
            self.reservation.provider_effect_phase,
        )

    def test_progress_survives_runner_retry_and_rejects_old_attempt(self) -> None:
        self.save_progress()
        marked = self.store.mark_mutation_reconcile_required(
            reservation=self.reservation,
            reconciliation_key=self.reservation.reconciliation_key,
        ).record
        assert marked is not None
        retried = self.store.retry_reconciled_mutation(
            reservation=marked,
            lease_owner="resuming-worker",
        ).record
        assert retried is not None
        self.assertEqual(retried.provider_effect_phase, "")
        self.assertEqual(
            read_state_volume_progress(self.store, self.scope, self.intent["plan_id"]),
            self.reservation.provider_effect_phase,
        )
        with self.assertRaises(ValueError):
            self.save_progress()
        updated = self.store.checkpoint_mutation_provider_effect(
            reservation=retried,
            effect_phase='{"project":"original-project"}',
        ).record
        assert updated is not None
        self.save_progress(updated)
        self.assertEqual(
            read_state_volume_progress(self.store, self.scope, self.intent["plan_id"]),
            updated.provider_effect_phase,
        )

    def test_progress_is_bound_to_plan_and_reviewed_custody(self) -> None:
        before = self.digest
        self.save_progress()
        self.assertNotEqual(state_volume_custody_sha256(self.store, self.reservation), before)
        with self.expired(), self.assertRaises(ValueError):
            self.retain()
        with self.assertRaises(ValueError):
            checkpoint_state_volume_progress(
                self.store,
                reservation=self.reservation,
                plan_id="b" * 32,
                phase=self.reservation.provider_effect_phase,
            )

    def test_retained_plan_cannot_receive_progress_from_a_fresh_operation_key(self) -> None:
        self.assertFalse(
            state_volume_plan_is_retained(self.store, self.scope, self.intent["plan_id"])
        )
        self.save_progress()
        self.digest = state_volume_custody_sha256(self.store, self.reservation)
        with self.expired():
            self.retain()
        fresh = self.store.reserve_mutation(
            scope=self.scope,
            route_path=ROUTE,
            idempotency_key="new-key-same-old-plan",
            request_fingerprint=self.intent["plan_sha256"],
            lease_owner="fresh-worker",
            reconciliation_key=self.intent["plan_sha256"],
            provider_target_key=self.reservation.provider_target_key,
            original_intent=self.intent,
        ).record
        assert fresh is not None
        checkpointed = self.store.checkpoint_mutation_provider_effect(
            reservation=fresh,
            effect_phase="unwanted-create",
        ).record
        assert checkpointed is not None
        with self.assertRaises(ValueError):
            self.save_progress(checkpointed)
        self.assertTrue(
            state_volume_plan_is_retained(self.store, self.scope, self.intent["plan_id"])
        )
        self.assertEqual(
            read_state_volume_progress(self.store, self.scope, self.intent["plan_id"]),
            self.reservation.provider_effect_phase,
        )

    def test_successful_completion_is_never_relabelled_as_retained(self) -> None:
        completed = self.store._updated_idempotency_record(
            self.reservation,
            state="completed",
            response_status_code=202,
            response_trace_id="created",
            recorded_at=self.reservation.updated_at,
            response_payload={"result": {"applied": True}},
        )
        self.store.write_idempotency_record(completed)
        with self.expired(), self.assertRaises(ValueError):
            self.retain()
        self.assertEqual(self.read(), completed)

    def test_completed_allocation_progress_rejects_a_different_operation_key(self) -> None:
        self.save_progress()
        completed = self.store._updated_idempotency_record(
            self.reservation,
            state="completed",
            response_status_code=202,
            response_trace_id="created",
            recorded_at=self.reservation.updated_at,
            response_payload={"result": {"applied": True}},
        )
        self.store.write_idempotency_record(completed)
        fresh = self.store.reserve_mutation(
            scope=self.scope,
            route_path=ROUTE,
            idempotency_key="new-key-completed-plan",
            request_fingerprint=self.intent["plan_sha256"],
            lease_owner="fresh-worker",
            reconciliation_key=self.intent["plan_sha256"],
            provider_target_key=self.reservation.provider_target_key,
            original_intent=self.intent,
        ).record
        assert fresh is not None
        checkpointed = self.store.checkpoint_mutation_provider_effect(
            reservation=fresh,
            effect_phase="unwanted-create",
        ).record
        assert checkpointed is not None
        with self.assertRaises(ValueError):
            self.save_progress(checkpointed)
        self.assertEqual(
            state_volume_progress_operation_key(self.store, self.scope, self.intent["plan_id"]),
            self.reservation.idempotency_key,
        )
        self.assertEqual(
            read_state_volume_progress(self.store, self.scope, self.intent["plan_id"]),
            self.reservation.provider_effect_phase,
        )

    def test_other_route_or_target_cannot_use_retention(self) -> None:
        for changes in (
            {"route_path": "/v1/another-operation"},
            {"provider_target_key": "some-other-target"},
            {"scope": "terminal-agent:another-caller"},
        ):
            with self.subTest(changes=changes):
                unrelated = self.reservation.model_copy(update=changes)
                with self.expired(), self.assertRaises(ValueError):
                    self.retain(
                        reservation=unrelated,
                        expected_custody_sha256=request_fingerprint(
                            unrelated.model_dump(mode="json")
                        ),
                    )
        self.assertEqual(self.read(), self.reservation)
