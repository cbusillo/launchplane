"""Focused native schedule/publication integration on isolated PostgreSQL databases."""

import unittest
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from threading import Thread

from sqlalchemy import create_engine, text
from psycopg import Connection

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
        from control_plane.storage.testing_native_disposal import dispatch_native_disposal

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
        with self.assertRaisesRegex(DrillPrerequisite, "native_testing_copy_protected"):
            dispatch_native_disposal(
                fixture.store,
                operation,
                identity,
                publication,
                effect,
                lambda: self.fail("Retained copy was dispatched"),
            )

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
