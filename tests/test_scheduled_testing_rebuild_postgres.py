"""Focused native schedule/publication integration on isolated PostgreSQL databases."""

import unittest
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from threading import Thread

from sqlalchemy import create_engine, text
from psycopg import Connection

from control_plane.dokploy.api import dokploy_request
from tests import test_scheduled_testing_rebuild as scheduled
from tests import test_odoo_testing_drill as drills
from tests import test_testing_native_maintenance as maintenance_tests
from tests.test_postgres_integration import _head_postgres_database


class ScheduledNativePostgresTests(unittest.TestCase):
    def fixture(self) -> scheduled.ScheduledNativeRebuildTests:
        fixture = scheduled.ScheduledNativeRebuildTests()
        fixture.database_url = self.enterContext(_head_postgres_database())
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_two_scheduled_copies_and_next_deploy_preserve_all_serving_authority(self) -> None:
        self.fixture().test_two_scheduled_native_restores_preserve_address_original_witnesses_and_production()

    def test_original_storage_recovery_precedes_fresh_capacity(self) -> None:
        self.fixture().test_interrupted_original_storage_is_read_before_capacity_without_reallocation()

    def test_custody_history_retains_true_count_with_bounded_reads(self) -> None:
        self.fixture().test_custody_history_has_truthful_totals_and_bounded_database_reads()

    def test_five_daily_copies_with_ordinary_deploys_keep_bounded_physical_custody(self) -> None:
        self.fixture().test_five_scheduled_copies_reclaim_older_drills_and_preserve_manual_baseline()

    def test_manual_native_successor_preserves_original_drill_collection_proof(self) -> None:
        self.fixture().test_manual_native_successor_qualifies_original_drill_for_later_collection()

    def test_original_retirement_continuation_preserves_its_effect_fence(self) -> None:
        self.fixture().test_original_retirement_resumes_only_after_host_no_dispatch_exclusion()

    def test_disposal_cp_clock_cannot_exclude_live_host_admission(self) -> None:
        self.fixture().test_disposal_cp_clock_jump_cannot_exclude_live_host_admission()

    def test_retention_cannot_exclude_uncertain_stop_or_authorize_inverse(self) -> None:
        self.fixture().test_uncertain_original_retirement_cannot_rollback_or_borrow_retention()

    def test_retirement_missing_next_admission_continues_the_original_family(self) -> None:
        self.fixture().test_retirement_attempt_without_admission_recovers_the_original_family()

    def test_disposal_missing_next_admission_continues_the_original_family(self) -> None:
        self.fixture().test_disposal_attempt_without_admission_reuses_the_complete_original_family()

    def test_recent_original_does_not_skip_other_qualified_copies(self) -> None:
        self.fixture().test_recent_old_copy_does_not_skip_another_eligible_original()

    def test_host_preserved_forward_outcome_stays_on_the_healthy_publication(self) -> None:
        self.fixture().test_real_host_preservation_completes_healthy_publication_after_lost_cp_close()

    def test_host_no_stop_exclusion_allows_inverse_and_another_native_retirer(self) -> None:
        self.fixture().test_real_host_no_stop_allows_original_inverse_and_a_new_native_retirer()

    def test_native_rollback_closure_remains_sticky_after_health_recovers(self) -> None:
        self.fixture().test_native_rollback_exclusion_survives_lost_reply_and_recovered_new_health()

    def test_late_shared_receipt_exposure_remains_untrusted_after_mount_removal(self) -> None:
        self.fixture().test_receipt_exposure_remains_untrusted_after_the_mount_is_removed()

    def test_unknown_failed_original_allocation_blocks_new_capture(self) -> None:
        self.fixture().test_unknown_failed_allocation_blocks_another_physical_capture()

    def test_explicit_namespace_retention_keeps_failure_custody_and_supports_continuation(
        self,
    ) -> None:
        self.fixture().test_explicit_original_namespace_retention_supports_continuing_after_failure()

    def test_original_copy_sql_counts_privileged_and_other_account_logins(self) -> None:
        fixture = maintenance_tests.NativeMaintenanceHostTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        engine = create_engine(self.enterContext(_head_postgres_database()))
        self.addCleanup(engine.dispose)
        latest = datetime.fromtimestamp(fixture.now - 60, UTC).replace(tzinfo=None)
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE ir_config_parameter (key text PRIMARY KEY,value text NOT NULL)")
            )
            connection.execute(
                text(
                    "CREATE TABLE res_users_log (create_uid integer NOT NULL,create_date timestamp NOT NULL)"
                )
            )
            connection.execute(
                text("INSERT INTO ir_config_parameter VALUES (:key,:value)"),
                {"key": fixture.spec["copy_witness_parameter"], "value": fixture.witness},
            )
            connection.execute(
                text("INSERT INTO res_users_log VALUES (:user_id,:created)"),
                [
                    {"user_id": 77, "created": latest - timedelta(minutes=20)},
                    {"user_id": 1, "created": latest},
                    {"user_id": 2, "created": latest - timedelta(days=2)},
                ],
            )
        original = fixture.docker

        def command(*arguments: str, stdin: bytes | None = None, **kwargs: Any) -> bytes:
            if arguments[0] != "exec":
                return original(*arguments, stdin=stdin, **kwargs)
            original(*arguments, stdin=stdin, **kwargs)
            assert stdin is not None
            connection = engine.raw_connection()
            driver = connection.driver_connection
            assert isinstance(driver, Connection)
            driver.autocommit = True
            try:
                result = None
                with driver.cursor() as cursor:
                    for statement in stdin.decode().split(";"):
                        if not statement.strip():
                            continue
                        bound = statement.replace(":'witness_key'", "%(witness_key)s").replace(
                            ":'prepared'", "%(prepared)s"
                        )
                        parameters = {"witness_key": arguments[-2], "prepared": arguments[-1]}
                        cursor.execute(bound, parameters if "%(" in bound else None)
                        if cursor.description is not None:
                            row = cursor.fetchone()
                            assert row is not None
                            result = row[0]
                return json.dumps(result).encode()
            finally:
                connection.close()

        fixture.host["docker"] = command
        with patch.object(fixture, "docker", side_effect=command):
            self.assertEqual(fixture.run_mode("qualify-retirement")["status"], "recent_use")
        receipt = json.loads(next(fixture.state.glob("native-*.json")).read_text())
        self.assertEqual(
            datetime.fromisoformat(receipt["max_login_at"]), latest.replace(tzinfo=UTC)
        )

    def test_retention_edit_serializes_with_the_actual_host_send_and_then_refuses(self) -> None:
        from control_plane.contracts.odoo_testing_drill import NATIVE_DRILL_MAINTENANCE_ROUTE
        from control_plane.odoo_testing_drill import DRILL_SCOPE, DrillPrerequisite
        from control_plane.storage.postgres import PostgresRecordStore
        from control_plane.storage.testing_native_disposal import (
            dispatch_native_disposal,
            register_native_admission,
        )

        fixture = self.fixture()
        fixture.refuse_after_writer_dispatch()
        operation = fixture.worker.operation
        namespace = fixture.worker.fixture.candidate.namespace
        identity = {
            "namespace": namespace,
            "target_id": "unserved-native-original",
            "copy_binding": "c" * 64,
        }
        profile = fixture.store.read_product_profile_record(operation.product)
        retained = profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(
                        update={
                            "odoo_testing_drill_storage": fixture.budget.model_copy(
                                update={"retained_candidate_namespaces": (namespace,)}
                            )
                        }
                    )
                    if lane.instance == "testing"
                    else lane
                    for lane in profile.lanes
                )
            }
        )
        effect = fixture.store.reserve_mutation(
            scope=DRILL_SCOPE,
            route_path=NATIVE_DRILL_MAINTENANCE_ROUTE,
            idempotency_key=namespace,
            request_fingerprint="d" * 64,
            lease_owner="native-disposal-fixture",
            lease_seconds=300,
            provider_target_key="native-disposal-fixture",
            reconciliation_key="d" * 64,
        ).record
        assert effect is not None
        register_native_admission(
            fixture.store,
            effect,
            NATIVE_DRILL_MAINTENANCE_ROUTE + ":attempt",
            [],
            {
                "identity_sha256": "d" * 64,
                "host_boot_id": "fixture-boot",
                "host_expires_at": 120.0,
                "expires_at": datetime.now(UTC).timestamp() + 120,
                "namespace_sha256": "e" * 64,
            },
        )
        binding = fixture.worker.intent.before.binding
        assert binding is not None
        publication = binding.source.source_versions["testing_publication"]
        writer = PostgresRecordStore(
            database_url=fixture.store.database_url, postgres_statement_timeout_milliseconds=250
        )
        self.addCleanup(writer.close)
        failures = []

        def update() -> None:
            try:
                writer.write_product_profile_record(retained)
            except Exception as error:
                failures.append(error)

        def send() -> None:
            concurrent = Thread(target=update)
            concurrent.start()
            concurrent.join(5)
            self.assertFalse(concurrent.is_alive())
            self.assertEqual(len(failures), 1)
            self.assertEqual(getattr(getattr(failures[0], "orig", None), "sqlstate", None), "57014")

        dispatch_native_disposal(fixture.store, operation, identity, publication, effect, send)
        writer.write_product_profile_record(retained)
        with self.assertRaises(DrillPrerequisite) as refused:
            dispatch_native_disposal(
                fixture.store,
                operation,
                identity,
                publication,
                effect,
                lambda: self.fail("Retained copy was dispatched"),
            )
        self.assertEqual(refused.exception.code, "native_testing_copy_protected")

    def test_native_admission_serializes_registration_supersession_and_terminal_close(self) -> None:
        from control_plane.contracts.testing_rebuild import REBUILD_SCOPE, REBUILD_ROUTE
        from control_plane.odoo_testing_drill import DrillPrerequisite
        from control_plane.storage.postgres import PostgresRecordStore
        from control_plane.storage import testing_native_disposal as native

        store = PostgresRecordStore(database_url=self.enterContext(_head_postgres_database()))
        self.addCleanup(store.close)
        writer = PostgresRecordStore(
            database_url=store.database_url, postgres_statement_timeout_milliseconds=250
        )
        self.addCleanup(writer.close)
        child = store.reserve_mutation(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key="native-race:retire-old",
            request_fingerprint="a" * 64,
            lease_owner="original-owner",
            lease_seconds=300,
            provider_target_key="native-original",
            reconciliation_key="a" * 64,
            original_intent={"native_retirement": {"identity_sha256": "b" * 64}},
        ).record
        assert child is not None
        renewed = writer.renew_mutation_reservation(reservation=child, lease_seconds=600).record
        assert renewed is not None
        self.assertNotEqual(renewed.lease_expires_at, child.lease_expires_at)
        route = REBUILD_ROUTE + ":retirement-admission"
        values = {
            "identity_sha256": "b" * 64,
            "host_boot_id": "fixture-boot",
            "host_expires_at": 120.0,
            "expires_at": datetime.now(UTC).timestamp() + 120,
            "namespace_sha256": "c" * 64,
        }
        evidence = native._native_evidence
        failures: list[Exception] = []

        def contend(record: Any, action: Any) -> None:
            try:
                action(record)
            except Exception as error:
                failures.append(error)

        def registration_barrier(*args: Any, **kwargs: Any) -> Any:
            thread = Thread(
                target=contend,
                args=(
                    renewed,
                    lambda record: writer.mark_mutation_reconcile_required(
                        reservation=record, reconciliation_key=record.reconciliation_key
                    ),
                ),
            )
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(
                getattr(getattr(failures[-1], "orig", None), "sqlstate", None), "57014"
            )
            return evidence(*args, **kwargs)

        # A benign heartbeat is allowed; true supersession waits on the same
        # exact child row while an admission is being durably inserted.
        with patch.object(native, "_native_evidence", side_effect=registration_barrier):
            first = native.register_native_admission(store, child, route, [], values)
        self.assertEqual(first["attempt"], 1)
        with store._session_factory() as session:
            native.require_native_send(store, session, child, route)
        reconcile = store.mark_mutation_reconcile_required(
            reservation=renewed, reconciliation_key=renewed.reconciliation_key
        ).record
        assert reconcile is not None
        current = writer.retry_reconciled_mutation(
            reservation=reconcile, lease_owner="next-owner"
        ).record
        assert current is not None
        with self.assertRaises(DrillPrerequisite):
            native.register_native_admission(store, child, route, [first], values)
        family = native.native_admission_family(store, current, route)
        self.assertEqual(family, [first])
        second = native.register_native_admission(
            store, current, route, family, {**values, "host_expires_at": 110.0}
        )
        family2 = native.native_admission_family(store, current, route)
        self.assertEqual({item["attempt"] for item in family2}, {1, 2})
        self.assertLess(second["host_expires_at"], first["host_expires_at"])
        from control_plane.odoo_testing_native_maintenance import _family_excluded

        between = (second["host_expires_at"] + first["host_expires_at"]) / 2
        observation = {"host_boot_id": first["host_boot_id"], "host_monotonic": between}
        self.assertFalse(_family_excluded(family2, observation))
        self.assertTrue(
            _family_excluded(
                family2,
                {
                    **observation,
                    "host_monotonic": max(item["host_expires_at"] for item in family2) + 1,
                },
            )
        )
        proof = {
            "status": "preserved",
            "no_stop_dispatched": True,
            "identity_sha256": "b" * 64,
            "namespace_sha256": "c" * 64,
        }
        with self.assertRaises(DrillPrerequisite):
            native.close_native_admissions(store, current, route, family, proof)
        self.assertIsNone(native.native_closure(store, current))
        with self.assertRaises(DrillPrerequisite):
            native.close_native_admissions(
                store, current, route, family2, {**proof, "identity_sha256": "d" * 64}
            )
        failures.clear()

        def closure_barrier(*args: Any, **kwargs: Any) -> Any:
            thread = Thread(
                target=contend,
                args=(
                    current,
                    lambda record: native.register_native_admission(
                        writer, record, route, family2, values
                    ),
                ),
            )
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(
                getattr(getattr(failures[-1], "orig", None), "sqlstate", None), "57014"
            )
            return evidence(*args, **kwargs)

        with patch.object(native, "_native_evidence", side_effect=closure_barrier):
            native.close_native_admissions(store, current, route, family2, proof)
        initial = native.native_closure(store, current)
        assert initial is not None
        native.close_native_admissions(
            store, current, route, family2, {**proof, "host_monotonic": 500.0}, rollback=True
        )
        closed = native.native_closure(store, current)
        assert closed is not None
        self.assertTrue(closed["rollback_requested"])
        self.assertEqual(closed["proof"], initial["proof"])
        with self.assertRaises(DrillPrerequisite):
            native.register_native_admission(writer, current, route, family2, values)
        with writer._session_factory() as session, self.assertRaises(DrillPrerequisite):
            native.require_native_send(writer, session, current, route)

        # A worker takeover between observation and the negative-proof lock
        # must not erase observed exposure of this same immutable original.
        loss = {"reason": "native_copy_receipt_untrusted", "namespace_sha256": "c" * 64}
        native.note_native_custody_loss(store, child, loss)
        self.assertTrue(native.native_custody_lost(writer, current))
        marker = store.read_idempotency_record(
            scope=child.scope,
            route_path=child.route_path + ":native-custody-loss",
            idempotency_key=child.record_id,
        )
        assert marker is not None
        native.note_native_custody_loss(writer, current, {**loss, "later": True})
        self.assertEqual(
            store.read_idempotency_record(
                scope=child.scope,
                route_path=child.route_path + ":native-custody-loss",
                idempotency_key=child.record_id,
            ),
            marker,
        )
        with self.assertRaises(DrillPrerequisite):
            native.note_native_custody_loss(
                store, child.model_copy(update={"request_fingerprint": "e" * 64}), loss
            )

    def test_native_retirement_send_serializes_a_supported_hold_and_refuses_after_commit(
        self,
    ) -> None:
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
        from control_plane.contracts.testing_rebuild import REBUILD_SCOPE, REBUILD_ROUTE
        from control_plane.odoo_testing_drill import DrillPrerequisite
        from control_plane.storage.postgres import PostgresRecordStore
        from control_plane.storage.testing_native_disposal import (
            register_native_admission,
            dispatch_native_retirement,
            dispatch_testing_retirement,
        )

        fixture = self.fixture()
        fixture.refuse_after_writer_dispatch()
        operation = fixture.worker.operation
        child = fixture.store.reserve_mutation(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key="held-original:retire-old",
            request_fingerprint="a" * 64,
            lease_owner="native-hold-fixture",
            lease_seconds=300,
            provider_target_key="held-native-original",
            reconciliation_key="a" * 64,
        ).record
        assert child is not None
        route = REBUILD_ROUTE + ":retirement-admission"
        register_native_admission(
            fixture.store,
            child,
            route,
            [],
            {
                "host_boot_id": "fixture-boot",
                "host_expires_at": 120.0,
                "expires_at": datetime.now(UTC).timestamp() + 120,
                "namespace_sha256": "e" * 64,
            },
        )
        target = fixture.store.read_dokploy_target_record(
            context_name=operation.context, instance_name="testing"
        )
        held = target.model_copy(
            update={
                "policies": target.policies.model_copy(
                    update={
                        "staff_testing_hold": DokployTargetStaffTestingHold(
                            reason="fixture staff session",
                            recorded_at=datetime.now(UTC).isoformat(),
                        )
                    }
                )
            }
        )
        writer = PostgresRecordStore(
            database_url=fixture.store.database_url, postgres_statement_timeout_milliseconds=250
        )
        self.addCleanup(writer.close)
        failures: list[Exception] = []

        def update() -> None:
            try:
                writer.compare_and_write_dokploy_target_record(
                    expected_record=target, replacement_record=held
                )
            except Exception as error:
                failures.append(error)

        def send() -> None:
            thread = Thread(target=update)
            thread.start()
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(
                getattr(getattr(failures[-1], "orig", None), "sqlstate", None), "57014"
            )

        dispatch_native_retirement(fixture.store, operation, child, route, send)
        from control_plane.dokploy import api

        parent = fixture.store.reserve_mutation(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE,
            idempotency_key="generic-hold-parent",
            request_fingerprint="f" * 64,
            lease_owner="generic-parent",
            lease_seconds=300,
            provider_target_key="generic-hold-parent",
            reconciliation_key="f" * 64,
        ).record
        assert parent is not None
        checkpoints = []
        observations = []

        def observe(_host: str, path: str, _payload: Any, request: Any) -> Any:
            observations.append(path)
            return request()

        def checkpoint_and_send() -> None:
            parent_checkpoint = fixture.store.checkpoint_mutation_provider_effect(
                reservation=parent, effect_phase="retire-old"
            )
            self.assertEqual(parent_checkpoint.status, "updated")
            checkpoint = fixture.store.checkpoint_mutation_provider_effect(
                reservation=child, effect_phase="retire-old"
            )
            self.assertEqual(checkpoint.status, "updated")
            checkpoints.append(checkpoint.record)
            send()
            dokploy_request(
                host="fixture.invalid",
                token="fixture",
                path="/api/compose.stop",
                method="POST",
                payload={"composeId": "original"},
            )

        outer = getattr(fixture.store._disposable_authority_context, "session", None)
        with (
            api.observe_dokploy_mutations(observe),
            patch.object(api, "_dokploy_request", return_value={}),
        ):
            dispatch_testing_retirement(fixture.store, operation, checkpoint_and_send)
        self.assertEqual(observations, ["/api/compose.stop"])
        self.assertIs(getattr(fixture.store._disposable_authority_context, "session", None), outer)

        def lost_send() -> None:
            dokploy_request(
                host="fixture.invalid",
                token="fixture",
                path="/api/compose.stop",
                method="POST",
                payload={"composeId": "original"},
            )

        with (
            api.observe_dokploy_mutations(observe),
            patch.object(
                api,
                "_dokploy_request",
                side_effect=TimeoutError("fixture actual provider reply lost"),
            ),
            self.assertRaises(TimeoutError),
        ):
            dispatch_testing_retirement(fixture.store, operation, lost_send)
        stored_child = fixture.store.read_idempotency_record(
            scope=child.scope, route_path=child.route_path, idempotency_key=child.idempotency_key
        )
        assert stored_child is not None
        self.assertTrue(stored_child.provider_effect_started_at)
        self.assertNotIn("not_dispatched", stored_child.response_payload)
        self.assertIs(getattr(fixture.store._disposable_authority_context, "session", None), outer)
        writer.compare_and_write_dokploy_target_record(
            expected_record=target, replacement_record=held
        )
        with self.assertRaises(DrillPrerequisite) as refused:
            dispatch_native_retirement(
                fixture.store,
                operation,
                child,
                route,
                lambda: self.fail("Held retirement was sent"),
            )
        self.assertEqual(refused.exception.code, "staff_testing_hold")
        with self.assertRaises(DrillPrerequisite):
            dispatch_testing_retirement(
                fixture.store, operation, lambda: self.fail("Held retirement checkpointed")
            )
        self.assertEqual(len(checkpoints), 1)
        self.assertIs(getattr(fixture.store._disposable_authority_context, "session", None), outer)
        fresh = fixture.store.reserve_mutation(
            scope=REBUILD_SCOPE,
            route_path=REBUILD_ROUTE + ":effect",
            idempotency_key="held-before-first-checkpoint",
            request_fingerprint="e" * 64,
            lease_owner="held-fixture",
            lease_seconds=300,
            provider_target_key="held-before-first-checkpoint",
            reconciliation_key="e" * 64,
        ).record
        assert fresh is not None

        def forbidden_checkpoint() -> None:
            fixture.store.checkpoint_mutation_provider_effect(
                reservation=fresh, effect_phase="retire-old"
            )

        with self.assertRaises(DrillPrerequisite):
            dispatch_testing_retirement(fixture.store, operation, forbidden_checkpoint)
        stored = fixture.store.read_idempotency_record(
            scope=fresh.scope, route_path=fresh.route_path, idempotency_key=fresh.idempotency_key
        )
        assert stored is not None
        self.assertFalse(stored.provider_effect_started_at)

    def test_changed_collected_generation_is_refused_by_locked_admission(self) -> None:
        fixture = drills.NativeTestingDrillTests()
        self.addCleanup(fixture.doCleanups)
        fixture.seed(self.enterContext(_head_postgres_database()), legacy_operations=False)
        fixture.test_locked_native_admission_refuses_a_generation_changed_after_collection()

    def test_attempt_scoped_down_admission_survives_expiry_but_new_positive_use_refuses(
        self,
    ) -> None:
        fixture = drills.NativeTestingDrillTests()
        self.addCleanup(fixture.doCleanups)
        fixture.seed(self.enterContext(_head_postgres_database()), legacy_operations=False)
        fixture.test_down_qualified_native_attempt_keeps_history_when_monitor_expires_but_new_use_wins()

    def test_reclaimed_native_attempt_collects_full_history_and_preserves_holds(self) -> None:
        fixture = drills.NativeTestingDrillTests()
        self.addCleanup(fixture.doCleanups)
        fixture.seed(self.enterContext(_head_postgres_database()), legacy_operations=False)
        fixture.test_native_attempt_retakes_full_history_on_reclaim_and_keeps_holds_live()

    def test_forward_read_after_preparing_prevents_new_login_cutover(self) -> None:
        self.fixture().test_new_login_after_route_preparing_is_checked_before_any_forward_write()

    def test_refusal_excludes_preparing_and_fences_its_stale_writer(self) -> None:
        self.fixture().test_snapshot_refusal_during_route_preparation_excludes_the_original_parent()
