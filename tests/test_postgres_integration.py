from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
import threading
import unittest
from uuid import uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.idempotency_record import build_launchplane_idempotency_record_id
from control_plane.contracts.odoo_stable_bootstrap import OdooStableBootstrapRequest
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
    OdooStableBootstrapOperationPhase,
    OdooStableBootstrapOperationStatus,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.schema_invariants import EXPECTED_ALEMBIC_HEAD_REVISION

POSTGRES_TEST_URL_ENV = "LAUNCHPLANE_TEST_POSTGRES_URL"
LOCK_WAIT_TIMEOUT = "1000ms"


def _postgres_root_database_url() -> str:
    database_url = os.environ.get(POSTGRES_TEST_URL_ENV, "").strip()
    if not database_url:
        raise unittest.SkipTest(f"{POSTGRES_TEST_URL_ENV} is not set")
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise unittest.SkipTest(f"{POSTGRES_TEST_URL_ENV} must use postgresql+psycopg")
    return database_url


def _alembic_config(database_url: str) -> AlembicConfig:
    config = AlembicConfig("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@contextmanager
def _isolated_postgres_database() -> Iterator[str]:
    root_database_url = _postgres_root_database_url()
    root_url = make_url(root_database_url)
    database_name = f"launchplane_test_{uuid4().hex}"
    database_url = root_url.set(database=database_name).render_as_string(hide_password=False)
    root_engine = create_engine(root_database_url, isolation_level="AUTOCOMMIT")
    try:
        with root_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database_name}"'))
        try:
            yield database_url
        finally:
            with root_engine.connect() as connection:
                connection.execute(
                    text(
                        "select pg_terminate_backend(pid) "
                        "from pg_stat_activity where datname = :database_name"
                    ),
                    {"database_name": database_name},
                )
                connection.execute(text(f'DROP DATABASE IF EXISTS "{database_name}"'))
    finally:
        root_engine.dispose()


def _upgrade_empty_database_to_head(database_url: str) -> None:
    alembic_command.upgrade(_alembic_config(database_url), "head")


@contextmanager
def _store_for_fresh_head_database() -> Iterator[PostgresRecordStore]:
    with _isolated_postgres_database() as database_url:
        _upgrade_empty_database_to_head(database_url)
        store = PostgresRecordStore(database_url=database_url)
        try:
            store.verify_schema()
            yield store
        finally:
            store.close()


def _bootstrap_operation(
    *,
    operation_id: str = "odoo-stable-bootstrap-cm-testing-20260517t000000z-base",
    idempotency_key: str = "bootstrap-cm-testing",
    status: OdooStableBootstrapOperationStatus = "pending",
    phase: OdooStableBootstrapOperationPhase = "created",
    created_at: str = "2026-05-17T00:00:00Z",
    updated_at: str = "2026-05-17T00:00:00Z",
    lease_owner: str = "",
    lease_expires_at: str = "",
    heartbeat_at: str = "",
    attempt: int = 0,
    started_at: str = "",
    finished_at: str = "",
    error_message: str = "",
) -> OdooStableBootstrapOperationRecord:
    return OdooStableBootstrapOperationRecord(
        operation_id=operation_id,
        product="odoo-tenant-cm",
        context="cm",
        instance="testing",
        idempotency_key=idempotency_key,
        request_fingerprint=f"fingerprint-{idempotency_key}",
        request=OdooStableBootstrapRequest(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            confirmation="bootstrap cm testing",
        ),
        status=status,
        phase=phase,
        created_at=created_at,
        updated_at=updated_at,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        heartbeat_at=heartbeat_at,
        attempt=attempt,
        started_at=started_at or (updated_at if status == "running" else ""),
        finished_at=finished_at,
        error_message=error_message,
    )


def _idempotency_record(
    *, response_trace_id: str, request_fingerprint: str
) -> LaunchplaneIdempotencyRecord:
    return LaunchplaneIdempotencyRecord(
        record_id=build_launchplane_idempotency_record_id(response_trace_id=response_trace_id),
        scope="github-actions|cbusillo/launchplane|workflow:test",
        route_path="/v1/evidence/previews/generations",
        idempotency_key="preview-generation:launchplane:test:1",
        request_fingerprint=request_fingerprint,
        response_status_code=202,
        response_trace_id=response_trace_id,
        recorded_at="2026-07-01T00:00:00Z",
        response_payload={"status": "accepted", "trace_id": response_trace_id},
    )


class RealPostgresSchemaIntegrationTests(unittest.TestCase):
    def test_alembic_from_empty_database_reaches_exact_head_and_required_invariants(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                engine = store._engine
                inspector = inspect(engine)
                indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes(
                        "launchplane_odoo_stable_bootstrap_operations"
                    )
                }
                idempotency_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("launchplane_idempotency_records")
                }
                payload_type = _column_type(
                    engine,
                    table_name="launchplane_idempotency_records",
                    column_name="payload",
                )
                alembic_version = _current_alembic_version(engine)
            finally:
                store.close()

        self.assertEqual(alembic_version, EXPECTED_ALEMBIC_HEAD_REVISION)
        self.assertEqual(payload_type, "jsonb")
        self.assertTrue(
            idempotency_indexes["launchplane_idempotency_scope_route_key_idx"]["unique"]
        )
        self.assertTrue(indexes["launchplane_odoo_bootstrap_active_lane_uidx"]["unique"])

    def test_startup_verification_fails_closed_when_critical_index_is_missing(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("drop index launchplane_idempotency_scope_route_key_idx"))
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_idempotency_records missing required index",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_fails_closed_when_route_binding_index_is_missing(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("drop index launchplane_route_bindings_lookup_idx"))
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_route_bindings missing required index",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_fails_closed_when_route_binding_payload_is_not_jsonb(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "alter table launchplane_route_bindings "
                        "alter column payload type json using payload::json"
                    )
                )
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_route_bindings.payload has type",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_fails_closed_when_route_binding_primary_key_is_missing(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "alter table launchplane_route_bindings "
                        "drop constraint launchplane_route_bindings_pkey"
                    )
                )
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"launchplane_route_bindings has primary key \(<none>\)",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_fails_closed_when_partial_predicate_is_missing(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("drop index launchplane_odoo_bootstrap_active_lane_uidx"))
                connection.execute(
                    text(
                        "create unique index launchplane_odoo_bootstrap_active_lane_uidx "
                        "on launchplane_odoo_stable_bootstrap_operations "
                        "(product, context, instance)"
                    )
                )
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_odoo_bootstrap_active_lane_uidx has predicate",
                ):
                    store.verify_schema()
            finally:
                store.close()


class RealPostgresStorageConcurrencyTests(unittest.TestCase):
    def test_two_connections_claim_exactly_one_pending_operation_and_recover_lease(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            store.write_odoo_stable_bootstrap_operation_record(_bootstrap_operation())

            first_claim = store.claim_next_odoo_stable_bootstrap_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-05-17T00:10:00Z",
                claimed_at="2026-05-17T00:01:00Z",
            )
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                second_claim = second_store.claim_next_odoo_stable_bootstrap_operation_record(
                    lease_owner="worker-b",
                    lease_expires_at="2026-05-17T00:11:00Z",
                    claimed_at="2026-05-17T00:02:00Z",
                )
                stale_owner_heartbeat = (
                    second_store.heartbeat_odoo_stable_bootstrap_operation_record(
                        operation_id=first_claim.operation_id if first_claim else "missing",
                        lease_owner="worker-b",
                        heartbeat_at="2026-05-17T00:03:00Z",
                        lease_expires_at="2026-05-17T00:13:00Z",
                    )
                )
                recovered_ids = store.recover_expired_odoo_stable_bootstrap_operation_records(
                    now="2026-05-17T00:12:00Z",
                    safe_phases=("running",),
                    max_attempts=3,
                )
                recovered_claim = second_store.claim_next_odoo_stable_bootstrap_operation_record(
                    lease_owner="worker-b",
                    lease_expires_at="2026-05-17T00:22:00Z",
                    claimed_at="2026-05-17T00:13:00Z",
                )
            finally:
                second_store.close()

        self.assertIsNotNone(first_claim)
        assert first_claim is not None
        self.assertEqual(first_claim.lease_owner, "worker-a")
        self.assertIsNone(second_claim)
        self.assertFalse(stale_owner_heartbeat)
        self.assertEqual(recovered_ids, (first_claim.operation_id,))
        self.assertIsNotNone(recovered_claim)
        assert recovered_claim is not None
        self.assertEqual(recovered_claim.lease_owner, "worker-b")
        self.assertEqual(recovered_claim.attempt, 2)

    def test_row_lock_blocks_stale_owner_completion_until_claim_commits(self) -> None:
        with _store_for_fresh_head_database() as store:
            store.write_odoo_stable_bootstrap_operation_record(_bootstrap_operation())
            blocker = create_engine(store.database_url)
            stale_owner_result: list[bool | BaseException] = []
            worker_started = threading.Event()
            worker: threading.Thread | None = None
            try:
                with blocker.connect() as connection:
                    transaction = connection.begin()
                    try:
                        locked_row = connection.execute(
                            text(
                                "select operation_id "
                                "from launchplane_odoo_stable_bootstrap_operations "
                                "where status = 'pending' "
                                "for update"
                            )
                        ).fetchone()
                        self.assertIsNotNone(locked_row)
                        worker = threading.Thread(
                            target=_attempt_stale_owner_completion,
                            args=(store.database_url, stale_owner_result, worker_started),
                        )
                        worker.start()
                        self.assertTrue(worker_started.wait(timeout=5))
                        worker.join(timeout=5)
                        self.assertFalse(worker.is_alive())
                        self.assertEqual(len(stale_owner_result), 1)
                        lock_error = stale_owner_result[0]
                        self.assertIsInstance(lock_error, OperationalError)
                        assert isinstance(lock_error, OperationalError)
                        self.assertEqual(getattr(lock_error.orig, "sqlstate", ""), "55P03")
                    finally:
                        transaction.rollback()
                post_lock_result = store.complete_odoo_stable_bootstrap_operation_record(
                    record=_bootstrap_operation(
                        status="pass",
                        phase="completed",
                        updated_at="2026-05-17T00:04:00Z",
                        finished_at="2026-05-17T00:04:00Z",
                    ),
                    lease_owner="stale-worker",
                )
            finally:
                blocker.dispose()

        self.assertFalse(post_lock_result)

    def test_idempotency_unique_index_rejects_conflicting_two_connection_insert(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            first_record = _idempotency_record(
                response_trace_id="launchplane_req_first",
                request_fingerprint="fingerprint-first",
            )
            conflicting_record = _idempotency_record(
                response_trace_id="launchplane_req_second",
                request_fingerprint="fingerprint-second",
            )
            store.write_idempotency_record(first_record)
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                with self.assertRaises(IntegrityError):
                    second_store.write_idempotency_record(conflicting_record)
                loaded = second_store.read_idempotency_record(
                    scope=first_record.scope,
                    route_path=first_record.route_path,
                    idempotency_key=first_record.idempotency_key,
                )
            finally:
                second_store.close()

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.request_fingerprint, "fingerprint-first")

    def test_partial_unique_active_operation_index_rejects_second_active_lane(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            first_record = _bootstrap_operation(
                operation_id="odoo-stable-bootstrap-cm-testing-first",
                idempotency_key="bootstrap-first",
            )
            second_active_record = _bootstrap_operation(
                operation_id="odoo-stable-bootstrap-cm-testing-second",
                idempotency_key="bootstrap-second",
            )
            terminal_record = _bootstrap_operation(
                operation_id="odoo-stable-bootstrap-cm-testing-terminal",
                idempotency_key="bootstrap-terminal",
                status="fail",
                phase="failed",
                created_at="2026-05-17T00:02:00Z",
                updated_at="2026-05-17T00:02:00Z",
                finished_at="2026-05-17T00:02:00Z",
                error_message="terminal record does not reserve active lane",
            )
            store.write_odoo_stable_bootstrap_operation_record(first_record)
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                existing_record, created = (
                    second_store.create_odoo_stable_bootstrap_operation_record_if_no_active_lane(
                        second_active_record
                    )
                )
                second_store.write_odoo_stable_bootstrap_operation_record(terminal_record)
                terminal_records = second_store.list_odoo_stable_bootstrap_operation_records(
                    statuses=("fail",),
                )
            finally:
                second_store.close()

        self.assertFalse(created)
        self.assertEqual(existing_record.operation_id, first_record.operation_id)
        self.assertEqual(
            [record.operation_id for record in terminal_records], [terminal_record.operation_id]
        )


def _attempt_stale_owner_completion(
    database_url: str,
    results: list[bool | BaseException],
    started: threading.Event,
) -> None:
    worker_url = make_url(database_url).update_query_dict(
        {"options": f"-c lock_timeout={LOCK_WAIT_TIMEOUT}"}
    )
    store = PostgresRecordStore(database_url=worker_url.render_as_string(hide_password=False))
    try:
        started.set()
        result = store.complete_odoo_stable_bootstrap_operation_record(
            record=_bootstrap_operation(
                status="pass",
                phase="completed",
                updated_at="2026-05-17T00:04:00Z",
                finished_at="2026-05-17T00:04:00Z",
            ),
            lease_owner="stale-worker",
        )
        results.append(result)
    except BaseException as error:
        results.append(error)
    finally:
        store.close()


def _current_alembic_version(engine: Engine) -> str:
    with engine.connect() as connection:
        return str(connection.execute(text("select version_num from alembic_version")).scalar_one())


def _column_type(engine: Engine, *, table_name: str, column_name: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                text(
                    "select data_type from information_schema.columns "
                    "where table_schema = current_schema() "
                    "and table_name = :table_name and column_name = :column_name"
                ),
                {"table_name": table_name, "column_name": column_name},
            ).scalar_one()
        )


if __name__ == "__main__":
    unittest.main()
