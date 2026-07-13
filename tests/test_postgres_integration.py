from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
import threading
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, OperationalError

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_idempotency_record_id,
    build_launchplane_mutation_reservation,
    complete_launchplane_mutation_reservation,
)
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
)
from control_plane.contracts.odoo_stable_bootstrap import OdooStableBootstrapRequest
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
    OdooStableBootstrapOperationPhase,
    OdooStableBootstrapOperationStatus,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingDomain,
    RouteBindingIngress,
    RouteBindingProviderTarget,
    RouteBindingSource,
    RouteBindingTls,
)
from control_plane.storage.postgres import (
    DbOnlyMutationRequest,
    MutationReservationResult,
    PostgresRecordStore,
)
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


def _every_code_work_request() -> EveryCodeWorkRequestRecord:
    return EveryCodeWorkRequestRecord(
        request_id="every-code-cbusillo-code-1693-test",
        source="manual",
        state="queued",
        repository="cbusillo/code",
        issue_number=1693,
        issue_url="https://github.com/cbusillo/code/issues/1693",
        trigger_label="every-code",
        queued_at="2026-07-13T09:00:00Z",
        updated_at="2026-07-13T09:00:00Z",
    )


def _mutation_reservation(
    *,
    lease_owner: str,
    request_fingerprint: str = "mutation-fingerprint-a",
    idempotency_key: str = "product-preview-tls:postgres:1",
    lease_expires_at: str = "2026-07-13T00:05:00Z",
    reserved_at: str = "2026-07-13T00:00:00Z",
) -> LaunchplaneIdempotencyRecord:
    return build_launchplane_mutation_reservation(
        scope="github-actions|cbusillo/launchplane|workflow:test",
        route_path="/v1/product-profiles/preview-tls/apply",
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        reserved_at=reserved_at,
    )


def _mutation_completion(
    reservation: LaunchplaneIdempotencyRecord,
    *,
    response_trace_id: str,
) -> LaunchplaneIdempotencyRecord:
    return complete_launchplane_mutation_reservation(
        reservation,
        response_status_code=202,
        response_trace_id=response_trace_id,
        completed_at="2026-07-13T00:01:00Z",
        response_payload={"status": "accepted", "trace_id": response_trace_id},
    )


def _db_only_mutation(
    *,
    lease_owner: str,
    idempotency_key: str,
    response_trace_id: str,
) -> DbOnlyMutationRequest:
    return DbOnlyMutationRequest(
        scope="github-actions|cbusillo/launchplane|workflow:test",
        route_path="/v1/product-profiles/preview-tls/apply",
        idempotency_key=idempotency_key,
        request_fingerprint="mutation-fingerprint-a",
        lease_owner=lease_owner,
        response_status_code=202,
        response_trace_id=response_trace_id,
        response_payload={"status": "accepted", "trace_id": response_trace_id},
    )


def _reserve_mutation(
    store: PostgresRecordStore,
    reservation: LaunchplaneIdempotencyRecord,
    *,
    lease_seconds: int = 300,
) -> MutationReservationResult:
    return store.reserve_mutation(
        scope=reservation.scope,
        route_path=reservation.route_path,
        idempotency_key=reservation.idempotency_key,
        request_fingerprint=reservation.request_fingerprint,
        lease_owner=reservation.lease_owner,
        lease_seconds=lease_seconds,
        reconciliation_key=reservation.reconciliation_key,
    )


def _product_profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="postgres-reservation-test",
        display_name="PostgreSQL Reservation Test",
        repository="example/postgres-reservation-test",
        driver_id="odoo",
        image=ProductImageProfile(),
        preview=ProductPreviewProfile(),
        updated_at="2026-07-13T00:00:00Z",
        source="test:postgres-integration",
    )


def _route_binding() -> EnvironmentRouteBindingRecord:
    return EnvironmentRouteBindingRecord(
        product="example-product",
        context="example-testing",
        instance="web",
        provider_target=RouteBindingProviderTarget(
            provider_id="dokploy",
            target_category="compose",
            provider_target_type="compose",
            target_name="example-target",
            provider_evidence={"target_record": "example-testing:web"},
        ),
        ingress=RouteBindingIngress(
            provider="npmplus",
            endpoint_key="example-edge",
            termination_kind="edge",
            provider_evidence={"audit_record": "audit-1"},
        ),
        domains=(RouteBindingDomain(domain_name="app.example.test", role="primary"),),
        tls=RouteBindingTls(
            owner="launchplane",
            provider_evidence={"audit_record": "audit-1"},
        ),
        source=RouteBindingSource(
            source_kind="operator",
            source_label="test",
            source_record_ids=("operator:test",),
            refreshed_at="2026-07-12T00:00:00Z",
            freshness_status="recorded",
        ),
        updated_at="2026-07-12T00:00:00Z",
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
                idempotency_columns = {
                    column["name"]: column
                    for column in inspector.get_columns("launchplane_idempotency_records")
                }
                payload_type = _column_type(
                    engine,
                    table_name="launchplane_idempotency_records",
                    column_name="payload",
                )
                attempt_type = _column_type(
                    engine,
                    table_name="launchplane_idempotency_records",
                    column_name="attempt",
                )
                alembic_version = _current_alembic_version(engine)
            finally:
                store.close()

        self.assertEqual(alembic_version, EXPECTED_ALEMBIC_HEAD_REVISION)
        self.assertEqual(payload_type, "jsonb")
        self.assertEqual(attempt_type, "integer")
        self.assertTrue(idempotency_columns["response_status_code"]["nullable"])
        self.assertTrue(
            idempotency_indexes["launchplane_idempotency_scope_route_key_idx"]["unique"]
        )
        self.assertFalse(idempotency_indexes["launchplane_idempotency_state_lease_idx"]["unique"])
        self.assertTrue(indexes["launchplane_odoo_bootstrap_active_lane_uidx"]["unique"])

    def test_mutation_reservation_migration_backfills_existing_postgres_rows(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(_alembic_config(database_url), "c9d1e3f5a7b9")
            legacy_payload = {
                "schema_version": 1,
                "record_id": "idempotency-postgres-legacy",
                "scope": "github-actions:postgres-legacy",
                "route_path": "/v1/evidence/previews/generations",
                "idempotency_key": "postgres-legacy-key",
                "request_fingerprint": "postgres-legacy-fingerprint",
                "response_status_code": 202,
                "response_trace_id": "postgres-legacy-trace",
                "recorded_at": "2026-07-12T00:00:00Z",
                "response_payload": {"status": "accepted"},
            }
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO launchplane_idempotency_records "
                        "(record_id, scope, route_path, idempotency_key, request_fingerprint, "
                        "response_status_code, response_trace_id, recorded_at, payload) "
                        "VALUES (:record_id, :scope, :route_path, :idempotency_key, "
                        ":request_fingerprint, :response_status_code, :response_trace_id, "
                        ":recorded_at, CAST(:payload AS jsonb))"
                    ),
                    {
                        "record_id": "idempotency-postgres-legacy",
                        "scope": "github-actions:postgres-legacy",
                        "route_path": "/v1/evidence/previews/generations",
                        "idempotency_key": "postgres-legacy-key",
                        "request_fingerprint": "postgres-legacy-fingerprint",
                        "response_status_code": 202,
                        "response_trace_id": "postgres-legacy-trace",
                        "recorded_at": "2026-07-12T00:00:00Z",
                        "payload": json.dumps(legacy_payload),
                    },
                )
            engine.dispose()

            _upgrade_empty_database_to_head(database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                loaded = store.read_idempotency_record(
                    scope="github-actions:postgres-legacy",
                    route_path="/v1/evidence/previews/generations",
                    idempotency_key="postgres-legacy-key",
                )
                with store._engine.connect() as connection:
                    promoted = (
                        connection.execute(
                            text(
                                "SELECT state, attempt, created_at, updated_at "
                                "FROM launchplane_idempotency_records "
                                "WHERE record_id = 'idempotency-postgres-legacy'"
                            )
                        )
                        .mappings()
                        .one()
                    )
            finally:
                store.close()

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.schema_version, 1)
        self.assertEqual(loaded.state, "completed")
        self.assertEqual(promoted["state"], "completed")
        self.assertEqual(promoted["attempt"], 1)
        self.assertEqual(promoted["created_at"], "2026-07-12T00:00:00Z")
        self.assertEqual(promoted["updated_at"], "2026-07-12T00:00:00Z")

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

    def test_startup_verification_fails_closed_when_reservation_lease_index_is_missing(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("drop index launchplane_idempotency_state_lease_idx"))
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_idempotency_state_lease_idx",
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
    def test_every_code_two_workers_claim_exactly_once(self) -> None:
        with _store_for_fresh_head_database() as store:
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def claim(
                active_store: PostgresRecordStore, host: str
            ) -> EveryCodeWorkRequestRecord | None:
                barrier.wait(timeout=5)
                return active_store.claim_every_code_work_request_record(
                    request_id=record.request_id,
                    host=host,
                    claimed_at="2026-07-13T09:01:00Z",
                )

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = (
                        executor.submit(claim, store, "worker-a"),
                        executor.submit(claim, second_store, "worker-b"),
                    )
                    results = tuple(future.result(timeout=10) for future in futures)
                loaded = store.read_every_code_work_request_record(record.request_id)
            finally:
                second_store.close()

        claims = tuple(result for result in results if result is not None)
        self.assertEqual(len(claims), 1)
        self.assertEqual(loaded.claimed_by_host, claims[0].claimed_by_host)
        self.assertEqual(loaded.fencing_token, 1)
        self.assertEqual(loaded.attempt, 1)

    def test_every_code_heartbeat_and_stale_recovery_are_fenced(self) -> None:
        with _store_for_fresh_head_database() as store:
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)
            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-a",
                claimed_at="2026-07-13T09:01:00Z",
                lease_seconds=60,
            )
            assert claimed is not None
            stale_snapshot = store.list_stale_every_code_work_request_records(
                as_of="2026-07-13T09:03:00Z"
            )[0]
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def heartbeat() -> bool:
                barrier.wait(timeout=5)
                return store.heartbeat_every_code_work_request_record(
                    request_id=record.request_id,
                    host="worker-a",
                    fencing_token=claimed.fencing_token,
                    heartbeat_at="2026-07-13T09:02:30Z",
                    lease_expires_at="2026-07-13T09:12:30Z",
                )

            def recover() -> EveryCodeWorkRequestRecord | None:
                barrier.wait(timeout=5)
                return second_store.recover_stale_every_code_work_request_record(
                    expected_record=stale_snapshot,
                    recovered_at="2026-07-13T09:03:00Z",
                )

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    heartbeat_future = executor.submit(heartbeat)
                    recover_future = executor.submit(recover)
                    heartbeat_result = heartbeat_future.result(timeout=10)
                    recovery_result = recover_future.result(timeout=10)
                loaded = store.read_every_code_work_request_record(record.request_id)
            finally:
                second_store.close()

        self.assertNotEqual(heartbeat_result, recovery_result is not None)
        if heartbeat_result:
            self.assertIsNone(recovery_result)
            self.assertEqual(loaded.state, "claimed")
            self.assertEqual(loaded.lease_expires_at, "2026-07-13T09:12:30Z")
        else:
            self.assertIsNotNone(recovery_result)
            self.assertEqual(loaded.state, "queued")

    def test_every_code_status_update_rejects_stale_fencing_token(self) -> None:
        with _store_for_fresh_head_database() as store:
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)
            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-a",
                claimed_at="2026-07-13T09:01:00Z",
            )
            assert claimed is not None
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                with self.assertRaisesRegex(ValueError, "fencing token"):
                    second_store.update_every_code_work_request_status_record(
                        request_id=record.request_id,
                        update=EveryCodeWorkRequestStatusUpdate(
                            state="done",
                            host="worker-a",
                            fencing_token=claimed.fencing_token + 1,
                            updated_at="2026-07-13T09:02:00Z",
                            result_summary="stale completion",
                        ),
                    )
                completed = store.update_every_code_work_request_status_record(
                    request_id=record.request_id,
                    update=EveryCodeWorkRequestStatusUpdate(
                        state="done",
                        host="worker-a",
                        fencing_token=claimed.fencing_token,
                        updated_at="2026-07-13T09:02:00Z",
                        result_summary="completed",
                    ),
                )
            finally:
                second_store.close()

        self.assertEqual(completed.state, "done")

    def test_every_code_claim_commits_replay_evidence_atomically(self) -> None:
        with _store_for_fresh_head_database() as store:
            record = _every_code_work_request()
            store.write_every_code_work_request_record(record)

            def idempotency_record_factory(
                claimed_record: EveryCodeWorkRequestRecord,
            ) -> LaunchplaneIdempotencyRecord:
                return LaunchplaneIdempotencyRecord(
                    record_id=build_launchplane_idempotency_record_id(
                        response_trace_id="trace-every-code-claim"
                    ),
                    scope="terminal-agent:every-code-worker",
                    route_path="/v1/every-code/work-requests/claim",
                    idempotency_key="every-code-claim-1693",
                    request_fingerprint="every-code-claim-fingerprint",
                    response_status_code=202,
                    response_trace_id="trace-every-code-claim",
                    recorded_at="2026-07-13T09:01:00Z",
                    response_payload={
                        "status": "accepted",
                        "result": {"request": claimed_record.model_dump(mode="json")},
                    },
                )

            claimed = store.claim_every_code_work_request_record(
                request_id=record.request_id,
                host="worker-a",
                claimed_at="2026-07-13T09:01:00Z",
                idempotency_record_factory=idempotency_record_factory,
            )
            replay_evidence = store.read_idempotency_record(
                scope="terminal-agent:every-code-worker",
                route_path="/v1/every-code/work-requests/claim",
                idempotency_key="every-code-claim-1693",
            )
            loaded = store.read_every_code_work_request_record(record.request_id)

        self.assertIsNotNone(claimed)
        self.assertIsNotNone(replay_evidence)
        self.assertEqual(loaded.state, "claimed")
        assert replay_evidence is not None
        self.assertEqual(replay_evidence.state, "completed")

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

    def test_two_store_instances_reserve_same_key_once_and_conflict_deterministically(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def reserve(
                active_store: PostgresRecordStore,
                reservation: LaunchplaneIdempotencyRecord,
            ) -> str:
                barrier.wait()
                return _reserve_mutation(active_store, reservation).status

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: reserve(*arguments),
                            (
                                (store, _mutation_reservation(lease_owner="worker-a")),
                                (second_store, _mutation_reservation(lease_owner="worker-b")),
                            ),
                        )
                    )
                conflict = _reserve_mutation(
                    second_store,
                    _mutation_reservation(
                        lease_owner="worker-c",
                        request_fingerprint="mutation-fingerprint-b",
                    ),
                )
                stored = store.read_idempotency_record(
                    scope="github-actions|cbusillo/launchplane|workflow:test",
                    route_path="/v1/product-profiles/preview-tls/apply",
                    idempotency_key="product-preview-tls:postgres:1",
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["acquired", "in_progress"])
        self.assertEqual(conflict.status, "conflict")
        self.assertIsNotNone(stored)
        assert stored is not None
        self.assertEqual(stored.state, "running")
        self.assertEqual(stored.attempt, 1)

    def test_expired_reconciliation_key_transitions_to_reconcile_required(self) -> None:
        with _store_for_fresh_head_database() as store:
            reservation = _mutation_reservation(lease_owner="worker-a")
            clock = {"now": "2026-07-13T00:00:00Z"}
            with patch.object(
                store,
                "_database_mutation_timestamp",
                side_effect=lambda _session: clock["now"],
            ):
                acquired = _reserve_mutation(store, reservation)
                clock["now"] = "2026-07-13T00:01:00Z"
                bound = store.bind_mutation_reconciliation_key(
                    reservation=acquired.record,
                    reconciliation_key="provider-operation-123",
                )
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                with patch.object(
                    second_store,
                    "_database_mutation_timestamp",
                    return_value="2026-07-13T00:06:00Z",
                ):
                    reconciled = _reserve_mutation(
                        second_store,
                        _mutation_reservation(lease_owner="worker-b"),
                    )
            finally:
                second_store.close()

        self.assertEqual(acquired.status, "acquired")
        self.assertEqual(bound.status, "updated")
        self.assertEqual(reconciled.status, "reconcile_required")
        self.assertEqual(reconciled.record.state, "reconcile_required")
        self.assertEqual(reconciled.record.reconciliation_key, "provider-operation-123")

    def test_expired_reclaim_fences_stale_attempt_across_store_instances(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            reservation = _mutation_reservation(lease_owner="worker-reused")
            try:
                acquired = _reserve_mutation(store, reservation, lease_seconds=1)
                time.sleep(1.1)
                reclaimed = _reserve_mutation(
                    second_store,
                    _mutation_reservation(lease_owner="worker-reused"),
                )
                stale_completion = _mutation_completion(
                    acquired.record,
                    response_trace_id="trace-stale-attempt",
                )
                stale_result = store.complete_mutation_reservation(
                    completion=stale_completion,
                )
            finally:
                second_store.close()

        self.assertEqual(acquired.status, "acquired")
        self.assertEqual(reclaimed.status, "acquired")
        self.assertEqual(reclaimed.record.attempt, 2)
        self.assertEqual(stale_result.status, "reservation_mismatch")

    def test_db_only_preflight_releases_expired_reservation_on_postgres(self) -> None:
        with _store_for_fresh_head_database() as store:
            mutation = _db_only_mutation(
                lease_owner="worker-b",
                idempotency_key="product-preview-tls:postgres:preflight-expired",
                response_trace_id="trace-worker-b",
            )
            acquired = store.reserve_mutation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
                lease_owner="worker-a",
                lease_seconds=1,
            )
            time.sleep(1.1)

            preflight = store.prepare_db_only_mutation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
            )
            stored_reservation = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )

        self.assertEqual(acquired.status, "acquired")
        self.assertEqual(preflight.status, "released")
        self.assertIsNone(stored_reservation)

    def test_atomic_noop_profile_mutation_replays_across_two_store_instances(self) -> None:
        with _store_for_fresh_head_database() as store:
            profile = _product_profile()
            store.write_product_profile_record(profile)
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def apply_noop(active_store: PostgresRecordStore, owner: str) -> str:
                mutation = _db_only_mutation(
                    lease_owner=owner,
                    idempotency_key="product-preview-tls:postgres:noop",
                    response_trace_id=f"trace-{owner}",
                )
                barrier.wait()
                return active_store.compare_and_write_product_profile_record(
                    expected_record=profile,
                    replacement_record=profile,
                    mutation=mutation,
                ).status

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: apply_noop(*arguments),
                            ((store, "worker-a"), (second_store, "worker-b")),
                        )
                    )
                stored_profile = store.read_product_profile_record(profile.product)
                stored_reservation = store.read_idempotency_record(
                    scope="github-actions|cbusillo/launchplane|workflow:test",
                    route_path="/v1/product-profiles/preview-tls/apply",
                    idempotency_key="product-preview-tls:postgres:noop",
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["replayed", "written"])
        self.assertEqual(stored_profile, profile)
        self.assertIsNotNone(stored_reservation)
        assert stored_reservation is not None
        self.assertEqual(stored_reservation.state, "completed")
        self.assertEqual(stored_reservation.attempt, 1)

    def test_atomic_profile_mutation_reclaims_expired_reservation_with_db_clock(
        self,
    ) -> None:
        with _store_for_fresh_head_database() as store:
            profile = _product_profile()
            mutation = _db_only_mutation(
                lease_owner="worker-b",
                idempotency_key="product-preview-tls:postgres:expired",
                response_trace_id="trace-worker-b",
            )
            store.write_product_profile_record(profile)
            acquired = store.reserve_mutation(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
                request_fingerprint=mutation.request_fingerprint,
                lease_owner="worker-a",
                lease_seconds=1,
            )
            time.sleep(1.1)

            result = store.compare_and_write_product_profile_record(
                expected_record=profile,
                replacement_record=profile,
                mutation=mutation,
            )
            stored_reservation = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )

        self.assertEqual(acquired.status, "acquired")
        self.assertEqual(result.status, "written")
        self.assertIsNotNone(stored_reservation)
        assert stored_reservation is not None
        self.assertEqual(stored_reservation.state, "completed")
        self.assertEqual(stored_reservation.attempt, 2)
        self.assertEqual(stored_reservation.lease_owner, mutation.lease_owner)
        self.assertEqual(stored_reservation.response_trace_id, mutation.response_trace_id)

    def test_route_binding_mutation_serializes_distinct_keys_across_stores(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            record = _route_binding()
            first_reservation = store.reserve_mutation(
                scope="github-actions:route-binding-test",
                route_path="/v1/route-bindings/backfill/apply",
                idempotency_key="route-binding-first",
                request_fingerprint="route-binding-fingerprint-first",
                lease_owner="worker-a",
            ).record
            second_reservation = second_store.reserve_mutation(
                scope="github-actions:route-binding-test",
                route_path="/v1/route-bindings/backfill/apply",
                idempotency_key="route-binding-second",
                request_fingerprint="route-binding-fingerprint-second",
                lease_owner="worker-b",
            ).record
            barrier = threading.Barrier(2)

            def create_binding(
                active_store: PostgresRecordStore,
                reservation: LaunchplaneIdempotencyRecord,
                trace_id: str,
            ) -> str:
                barrier.wait()
                return active_store.create_route_binding_record_with_mutation(
                    record=record,
                    reservation=reservation,
                    response_status_code=202,
                    response_trace_id=trace_id,
                    response_payload={"status": "accepted", "trace_id": trace_id},
                ).status

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: create_binding(*arguments),
                            (
                                (store, first_reservation, "trace-worker-a"),
                                (second_store, second_reservation, "trace-worker-b"),
                            ),
                        )
                    )
                stored_record = store.read_route_binding_record(
                    product=record.product,
                    context_name=record.context,
                    instance_name=record.instance,
                )
                reservation_records = tuple(
                    store.read_idempotency_record(
                        scope=reservation.scope,
                        route_path=reservation.route_path,
                        idempotency_key=reservation.idempotency_key,
                    )
                    for reservation in (first_reservation, second_reservation)
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["created", "exists"])
        self.assertEqual(stored_record, record)
        self.assertEqual(
            sorted(
                reservation.state if reservation is not None else "missing"
                for reservation in reservation_records
            ),
            ["completed", "missing"],
        )

    def test_profile_write_rolls_back_when_completion_persistence_fails(self) -> None:
        with _store_for_fresh_head_database() as store:
            profile = _product_profile()
            replacement = profile.model_copy(
                update={
                    "display_name": "Changed Before Injected Failure",
                    "updated_at": "2026-07-13T00:01:00Z",
                }
            )
            mutation = _db_only_mutation(
                lease_owner="worker-a",
                idempotency_key="product-preview-tls:postgres:fault",
                response_trace_id="trace-injected-failure",
            )
            store.write_product_profile_record(profile)

            with patch.object(
                store,
                "_sync_idempotency_row",
                side_effect=RuntimeError("injected completion persistence failure"),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "injected completion persistence failure",
                ):
                    store.compare_and_write_product_profile_record(
                        expected_record=profile,
                        replacement_record=replacement,
                        mutation=mutation,
                    )
            stored_profile = store.read_product_profile_record(profile.product)
            stored_reservation = store.read_idempotency_record(
                scope=mutation.scope,
                route_path=mutation.route_path,
                idempotency_key=mutation.idempotency_key,
            )

        self.assertEqual(stored_profile, profile)
        self.assertIsNone(stored_reservation)

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
