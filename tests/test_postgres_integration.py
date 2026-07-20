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
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    EveryCodeWorkRequestStatusUpdate,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerLeaseHeldError,
    MergeTrainControllerLeaseLostError,
    MergeTrainControllerStateRecord,
    build_merge_train_controller_key,
)
from control_plane.contracts.odoo_stable_bootstrap import OdooStableBootstrapRequest
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
    OdooStableBootstrapOperationPhase,
    OdooStableBootstrapOperationStatus,
)
from control_plane.contracts.outbox_delivery import (
    OutboxDeliveryRecord,
    build_outbox_delivery_id,
    build_outbox_dedupe_key,
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
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    run_durable_provider_operation,
)
from control_plane.service_auth import (
    GitHubActionsPolicyRule,
    LaunchplaneAuthzPolicy,
    LocalAdminPolicyRule,
)
from control_plane.storage.postgres import (
    DbOnlyMutationRequest,
    MutationReservationResult,
    OutboxWithIdempotencyRequest,
    PostgresRecordStore,
)
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.storage.schema_invariants import (
    AUTHZ_COMPATIBILITY_FLOOR_REVISION,
    EXPECTED_ALEMBIC_HEAD_REVISION,
    verify_postgres_schema_invariants,
)
from control_plane.storage.schema_migration import migrate_schema, schema_migration_action
from tests.support.artifact_manifests import artifact_manifest_v2

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


def _merge_train_controller_state(
    *,
    updated_at: str = "2026-07-13T00:00:00Z",
    lease_owner: str = "controller-a",
    lease_acquired_at: str = "2026-07-13T00:00:00Z",
    lease_expires_at: str = "2026-07-13T00:05:00Z",
    status: str = "running",
) -> MergeTrainControllerStateRecord:
    repository = "cbusillo/sellyouroutboard"
    base_branch = "main"
    return MergeTrainControllerStateRecord(
        controller_key=build_merge_train_controller_key(
            repository=repository,
            base_branch=base_branch,
        ),
        repository=repository,
        base_branch=base_branch,
        policy_key=f"{repository}:{base_branch}",
        policy_sha256="policy-sha",
        status=status,  # type: ignore[arg-type]
        updated_at=updated_at,
        lease_owner=lease_owner if status == "running" else "",
        lease_acquired_at=lease_acquired_at if status == "running" else "",
        lease_expires_at=lease_expires_at if status == "running" else "",
        heartbeat_at=updated_at if status == "running" else "",
        active_action="land_batch" if status == "running" else "",
        active_phase="cleanup_candidate_ref" if status == "running" else "",
        active_record_id="landing-record" if status == "running" else "",
        step_payload={"candidate_ref": "refs/heads/launchplane/train/x"},
        reconciliation_status="clean",
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


def _outbox_delivery(*, suffix: str = "one") -> OutboxDeliveryRecord:
    dedupe_key = build_outbox_dedupe_key(
        kind="github_workflow_dispatch",
        parts=("postgres", suffix),
    )
    return OutboxDeliveryRecord(
        delivery_id=build_outbox_delivery_id(
            kind="github_workflow_dispatch",
            dedupe_key=dedupe_key,
        ),
        kind="github_workflow_dispatch",
        aggregate_type="postgres_test",
        aggregate_id=suffix,
        dedupe_key=dedupe_key,
        created_at="2026-07-13T00:00:00Z",
        updated_at="2026-07-13T00:00:00Z",
        next_attempt_at="2026-07-13T00:00:00Z",
        payload={"repository": "example/repo", "workflow_id": "deploy.yml"},
    )


class RealPostgresSchemaIntegrationTests(unittest.TestCase):
    def test_full_release_upgrades_compatibility_floor_before_store_startup(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(
                _alembic_config(database_url),
                AUTHZ_COMPATIBILITY_FLOOR_REVISION,
            )
            migrated_revision = migrate_schema(database_url=database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                observed_revision = _current_alembic_version(store._engine)
            finally:
                store.close()

        self.assertEqual(migrated_revision, EXPECTED_ALEMBIC_HEAD_REVISION)
        self.assertEqual(observed_revision, EXPECTED_ALEMBIC_HEAD_REVISION)

    def test_f4_accepts_previous_writer_shape(self) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            policy = LaunchplaneAuthzPolicy(
                schema_version=2,
                local_admins=(
                    LocalAdminPolicyRule(
                        managed_set_id="operator.owner",
                        managed_rule_id="authz.admin",
                        subjects=("owner",),
                        token_labels=("owner-admin",),
                        products=("launchplane",),
                        contexts=("launchplane",),
                        actions=("authz_policy_grant.write",),
                    ),
                ),
            )
            first_record = LaunchplaneAuthzPolicyRecord(
                record_id="authz-compat-first",
                source="test:compat",
                updated_at="2026-07-18T00:00:00Z",
                policy=policy,
            )
            second_record = first_record.model_copy(
                update={
                    "record_id": "authz-compat-second",
                    "updated_at": "2026-07-18T00:01:00Z",
                }
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                with store._engine.begin() as connection:
                    for record in (first_record, second_record):
                        payload = record.model_dump(mode="json")
                        payload.pop("revision", None)
                        connection.execute(
                            text(
                                "insert into launchplane_authz_policies "
                                "(record_id, status, source, updated_at, policy_sha256, payload) "
                                "values (:record_id, :status, :source, :updated_at, "
                                ":policy_sha256, cast(:payload as jsonb))"
                            ),
                            {
                                "record_id": record.record_id,
                                "status": record.status,
                                "source": record.source,
                                "updated_at": record.updated_at,
                                "policy_sha256": record.policy_sha256,
                                "payload": json.dumps(payload),
                            },
                        )
                active_records = store.list_authz_policy_records(status="active")
                superseded_records = store.list_authz_policy_records(status="superseded")
                with store._engine.connect() as connection:
                    revisions = tuple(
                        connection.execute(
                            text(
                                "select revision from launchplane_authz_policies order by revision"
                            )
                        ).scalars()
                    )
            finally:
                store.close()

        self.assertEqual(revisions, (1, 2))
        self.assertEqual(
            tuple(record.record_id for record in active_records), ("authz-compat-second",)
        )
        self.assertEqual(
            tuple(record.record_id for record in superseded_records),
            ("authz-compat-first",),
        )
        self.assertEqual(
            active_records[0].policy.local_admins[0].managed_rule_id,
            "authz.admin",
        )

    def test_managed_writer_rejects_concurrent_stale_authz_replacement(self) -> None:
        with _isolated_postgres_database() as database_url:
            migrate_schema(database_url=database_url)
            base_rule = LocalAdminPolicyRule(
                managed_set_id="operator.owner",
                managed_rule_id="authz.admin",
                subjects=("owner",),
                token_labels=("owner-admin",),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=("authz_policy_grant.write",),
            )
            base_record = LaunchplaneAuthzPolicyRecord(
                record_id="authz-concurrent-base",
                source="test:concurrent",
                updated_at="2026-07-18T00:00:00Z",
                policy=LaunchplaneAuthzPolicy(schema_version=2, local_admins=(base_rule,)),
            )
            replacements = tuple(
                LaunchplaneAuthzPolicyRecord(
                    record_id=f"authz-concurrent-{suffix}",
                    revision=2,
                    source="test:concurrent",
                    updated_at=f"2026-07-18T00:0{position}:00Z",
                    policy=LaunchplaneAuthzPolicy(
                        schema_version=2,
                        local_admins=(
                            base_rule,
                            LocalAdminPolicyRule(
                                managed_set_id="test.concurrent",
                                managed_rule_id=f"grant.{suffix}",
                                subjects=(suffix,),
                                token_labels=(f"{suffix}-token",),
                                products=("launchplane",),
                                contexts=("launchplane",),
                                actions=("product_profile.read",),
                            ),
                        ),
                    ),
                )
                for position, suffix in enumerate(("a", "b"), start=1)
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                base_record = store.seed_authz_policy_if_absent(base_record)
                start_barrier = threading.Barrier(2)

                def compare_and_write(record: LaunchplaneAuthzPolicyRecord) -> bool:
                    start_barrier.wait(timeout=5)
                    result = store.compare_and_write_authz_policy_record(
                        expected_record=base_record,
                        replacement_record=record,
                    )
                    return result.status == "written"

                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = tuple(executor.map(compare_and_write, replacements))
                active_records = store.list_authz_policy_records(status="active")
            finally:
                store.close()

        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(len(active_records), 1)
        self.assertIn(active_records[0].record_id, {record.record_id for record in replacements})

    def test_schema_migration_serializes_concurrent_startups(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(
                _alembic_config(database_url),
                AUTHZ_COMPATIBILITY_FLOOR_REVISION,
            )
            first_migration_entered = threading.Event()
            release_first_migration = threading.Event()
            invocation_lock = threading.Lock()
            invocation_count = 0

            def delayed_schema_migration_action(
                *, current_revision: str, target_revision: str
            ) -> str:
                nonlocal invocation_count
                with invocation_lock:
                    invocation_count += 1
                    is_first_invocation = invocation_count == 1
                if is_first_invocation:
                    first_migration_entered.set()
                    if not release_first_migration.wait(timeout=5):
                        raise TimeoutError("Timed out waiting to release the first migration.")
                return schema_migration_action(
                    current_revision=current_revision,
                    target_revision=target_revision,
                )

            with patch(
                "control_plane.storage.schema_migration.schema_migration_action",
                side_effect=delayed_schema_migration_action,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first_future = executor.submit(
                        migrate_schema,
                        database_url=database_url,
                        target_revision=EXPECTED_ALEMBIC_HEAD_REVISION,
                    )
                    self.assertTrue(first_migration_entered.wait(timeout=5))
                    second_future = executor.submit(
                        migrate_schema,
                        database_url=database_url,
                        target_revision=EXPECTED_ALEMBIC_HEAD_REVISION,
                    )
                    time.sleep(0.1)
                    self.assertFalse(second_future.done())
                    release_first_migration.set()
                    revisions = (first_future.result(), second_future.result())
            engine = create_engine(database_url)
            try:
                observed_revision = _current_alembic_version(engine)
            finally:
                engine.dispose()

        self.assertEqual(revisions, (EXPECTED_ALEMBIC_HEAD_REVISION,) * 2)
        self.assertEqual(observed_revision, EXPECTED_ALEMBIC_HEAD_REVISION)

    def test_artifact_dependency_provenance_round_trips_jsonb(self) -> None:
        with _store_for_fresh_head_database() as store:
            manifest = artifact_manifest_v2()

            store.write_artifact_manifest(manifest)
            loaded = store.read_artifact_manifest(manifest.artifact_id)

            provenance = loaded.dependency_provenance
            expected_provenance = manifest.dependency_provenance
            assert provenance is not None
            assert expected_provenance is not None
            self.assertEqual(loaded.schema_version, 2)
            self.assertEqual(provenance.target_platforms, ("linux/amd64", "linux/arm64"))
            self.assertEqual(
                provenance.python_environments["linux/amd64"].packages_sha256,
                expected_provenance.python_environments["linux/amd64"].packages_sha256,
            )

    def test_artifact_dependency_provenance_migration_preserves_v1_payload(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(_alembic_config(database_url), "e1f3a5c7d9b1")
            legacy_artifact_id = "artifact-cm-v1-before-dependency-provenance"
            legacy_source_commit = "legacy-short-ref"
            legacy_image_repository = "ghcr.io/cbusillo/odoo-tenant-cm"
            legacy_image_digest = "sha256:legacy"
            legacy_payload = {
                "artifact_id": legacy_artifact_id,
                "source_commit": legacy_source_commit,
                "enterprise_base_digest": "sha256:legacy",
                "image": {
                    "repository": legacy_image_repository,
                    "digest": legacy_image_digest,
                },
            }
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO launchplane_artifact_manifests "
                        "(artifact_id, source_commit, image_repository, image_digest, payload) "
                        "VALUES (:artifact_id, :source_commit, :image_repository, :image_digest, "
                        "CAST(:payload AS jsonb))"
                    ),
                    {
                        "artifact_id": legacy_artifact_id,
                        "source_commit": legacy_source_commit,
                        "image_repository": legacy_image_repository,
                        "image_digest": legacy_image_digest,
                        "payload": json.dumps(legacy_payload),
                    },
                )
            engine.dispose()

            _upgrade_empty_database_to_head(database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                loaded = store.read_artifact_manifest(legacy_artifact_id)
            finally:
                store.close()

        self.assertEqual(loaded.schema_version, 1)
        self.assertIsNone(loaded.dependency_provenance)
        self.assertEqual(loaded.source_commit, "legacy-short-ref")

    def test_authz_policy_migration_canonicalizes_active_history_and_revisions(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(_alembic_config(database_url), "f3b5d7e9a1c2")
            policy = LaunchplaneAuthzPolicy(
                github_actions=(
                    GitHubActionsPolicyRule(
                        repository="cbusillo/launchplane",
                        actions=("product_profile.read",),
                    ),
                ),
            )
            records = (
                LaunchplaneAuthzPolicyRecord(
                    record_id="legacy-authz-older",
                    source="test:legacy",
                    updated_at="2026-07-17T00:00:00Z",
                    policy=policy,
                ),
                LaunchplaneAuthzPolicyRecord(
                    record_id="legacy-authz-newer",
                    source="test:legacy",
                    updated_at="2026-07-18T00:00:00Z",
                    policy=policy,
                ),
            )
            engine = create_engine(database_url)
            with engine.begin() as connection:
                for record in records:
                    payload = record.model_dump(mode="json")
                    payload.pop("revision", None)
                    connection.execute(
                        text(
                            "INSERT INTO launchplane_authz_policies "
                            "(record_id, status, source, updated_at, policy_sha256, payload) "
                            "VALUES (:record_id, :status, :source, :updated_at, :policy_sha256, "
                            "CAST(:payload AS jsonb))"
                        ),
                        {
                            "record_id": record.record_id,
                            "status": record.status,
                            "source": record.source,
                            "updated_at": record.updated_at,
                            "policy_sha256": record.policy_sha256,
                            "payload": json.dumps(payload),
                        },
                    )
            engine.dispose()

            _upgrade_empty_database_to_head(database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                active_records = store.list_authz_policy_records(status="active")
                superseded_records = store.list_authz_policy_records(status="superseded")
            finally:
                store.close()

        self.assertEqual(
            tuple((record.record_id, record.revision) for record in active_records),
            (("legacy-authz-newer", 2),),
        )
        self.assertEqual(
            tuple((record.record_id, record.revision) for record in superseded_records),
            (("legacy-authz-older", 1),),
        )

    def test_authz_policy_migration_rejects_tied_latest_active_records(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(_alembic_config(database_url), "f3b5d7e9a1c2")
            policy = LaunchplaneAuthzPolicy()
            engine = create_engine(database_url)
            with engine.begin() as connection:
                for record_id in ("legacy-authz-a", "legacy-authz-b"):
                    record = LaunchplaneAuthzPolicyRecord(
                        record_id=record_id,
                        source="test:legacy",
                        updated_at="2026-07-18T00:00:00Z",
                        policy=policy,
                    )
                    payload = record.model_dump(mode="json")
                    payload.pop("revision", None)
                    connection.execute(
                        text(
                            "INSERT INTO launchplane_authz_policies "
                            "(record_id, status, source, updated_at, policy_sha256, payload) "
                            "VALUES (:record_id, :status, :source, :updated_at, :policy_sha256, "
                            "CAST(:payload AS jsonb))"
                        ),
                        {
                            "record_id": record.record_id,
                            "status": record.status,
                            "source": record.source,
                            "updated_at": record.updated_at,
                            "policy_sha256": record.policy_sha256,
                            "payload": json.dumps(payload),
                        },
                    )
            engine.dispose()

            with self.assertRaisesRegex(RuntimeError, "share the latest updated_at timestamp"):
                _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            try:
                alembic_version = _current_alembic_version(engine)
            finally:
                engine.dispose()

        self.assertEqual(alembic_version, "f3b5d7e9a1c2")

    def test_authz_policy_write_fence_accepts_previous_binary_insert_shape(self) -> None:
        with _store_for_fresh_head_database() as store:
            current_record = store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="seed",
                    source="test:initial",
                    updated_at="2026-07-18T00:00:00Z",
                    policy=LaunchplaneAuthzPolicy(),
                )
            )
            legacy_record = LaunchplaneAuthzPolicyRecord(
                record_id="legacy-binary-active-write",
                source="test:legacy-binary",
                updated_at="2026-07-18T00:01:00Z",
                policy=LaunchplaneAuthzPolicy(
                    github_actions=(
                        GitHubActionsPolicyRule(
                            repository="example/legacy",
                            actions=("product_profile.read",),
                        ),
                    ),
                ),
            )
            legacy_payload = legacy_record.model_dump(mode="json")
            legacy_payload.pop("revision", None)
            engine = create_engine(store.database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO launchplane_authz_policies "
                        "(record_id, status, source, updated_at, policy_sha256, payload) "
                        "VALUES (:record_id, :status, :source, :updated_at, :policy_sha256, "
                        "CAST(:payload AS jsonb))"
                    ),
                    {
                        "record_id": legacy_record.record_id,
                        "status": legacy_record.status,
                        "source": legacy_record.source,
                        "updated_at": legacy_record.updated_at,
                        "policy_sha256": legacy_record.policy_sha256,
                        "payload": json.dumps(legacy_payload),
                    },
                )
                stored_payload = connection.execute(
                    text(
                        "select payload from launchplane_authz_policies "
                        "where record_id = :record_id"
                    ),
                    {"record_id": legacy_record.record_id},
                ).scalar_one()
            engine.dispose()
            active_records = store.list_authz_policy_records(status="active")
            superseded_records = store.list_authz_policy_records(status="superseded")

        self.assertEqual(active_records[0].record_id, legacy_record.record_id)
        self.assertEqual(active_records[0].revision, 2)
        self.assertEqual(
            superseded_records,
            (current_record.model_copy(update={"status": "superseded"}),),
        )
        self.assertNotIn("revision", stored_payload)

    def test_schema_verification_rejects_disabled_authz_policy_write_fence(self) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "ALTER TABLE launchplane_authz_policies DISABLE TRIGGER "
                            "launchplane_authz_policy_write_fence"
                        )
                    )
                with self.assertRaisesRegex(RuntimeError, "authz_policy_write_fence is disabled"):
                    verify_postgres_schema_invariants(engine)
            finally:
                engine.dispose()

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
                authz_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("launchplane_authz_policies")
                }
                authz_columns = {
                    column["name"]: column
                    for column in inspector.get_columns("launchplane_authz_policies")
                }
                outbox_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("launchplane_outbox_deliveries")
                }
                outbox_columns = {
                    column["name"]: column
                    for column in inspector.get_columns("launchplane_outbox_deliveries")
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
                outbox_payload_type = _column_type(
                    engine,
                    table_name="launchplane_outbox_deliveries",
                    column_name="payload",
                )
                outbox_attempt_type = _column_type(
                    engine,
                    table_name="launchplane_outbox_deliveries",
                    column_name="attempt",
                )
                outbox_max_attempts_type = _column_type(
                    engine,
                    table_name="launchplane_outbox_deliveries",
                    column_name="max_attempts",
                )
                alembic_version = _current_alembic_version(engine)
            finally:
                store.close()

        self.assertEqual(alembic_version, EXPECTED_ALEMBIC_HEAD_REVISION)
        self.assertEqual(payload_type, "jsonb")
        self.assertEqual(attempt_type, "integer")
        self.assertEqual(outbox_payload_type, "jsonb")
        self.assertEqual(outbox_attempt_type, "integer")
        self.assertEqual(outbox_max_attempts_type, "integer")
        self.assertTrue(idempotency_columns["response_status_code"]["nullable"])
        self.assertFalse(authz_columns["revision"]["nullable"])
        self.assertTrue(authz_indexes["launchplane_authz_policies_revision_uidx"]["unique"])
        self.assertTrue(authz_indexes["launchplane_authz_policies_active_uidx"]["unique"])
        self.assertFalse(outbox_columns["payload"]["nullable"])
        self.assertTrue(outbox_indexes["launchplane_outbox_deliveries_dedupe_uidx"]["unique"])
        self.assertFalse(outbox_indexes["launchplane_outbox_deliveries_claim_idx"]["unique"])
        self.assertTrue(
            idempotency_indexes["launchplane_idempotency_scope_route_key_idx"]["unique"]
        )
        self.assertFalse(idempotency_indexes["launchplane_idempotency_state_lease_idx"]["unique"])
        self.assertTrue(
            idempotency_indexes["launchplane_idempotency_active_reconciliation_idx"]["unique"]
        )
        self.assertEqual(
            idempotency_indexes["launchplane_idempotency_active_reconciliation_idx"][
                "column_names"
            ],
            ["provider_target_key"],
        )
        self.assertIn("provider_target_key", idempotency_columns)
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

    def test_provider_target_fence_migration_backfills_existing_active_rows(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(_alembic_config(database_url), "a2b4c6d8e0f2")
            reconciliation_key = "dokploy:compose:existing-provider-target"
            payload = {
                "schema_version": 2,
                "record_id": "mutation-reservation-existing-provider-target",
                "scope": "github-actions:provider-target-migration",
                "route_path": "/v1/test/provider-target",
                "idempotency_key": "provider-target-existing",
                "request_fingerprint": "provider-target-fingerprint",
                "state": "reconcile_required",
                "lease_owner": "worker-a",
                "lease_expires_at": "",
                "attempt": 1,
                "reconciliation_key": reconciliation_key,
                "provider_effect_phase": "",
                "provider_effect_started_at": "",
                "created_at": "2026-07-12T00:00:00Z",
                "updated_at": "2026-07-12T00:00:00Z",
                "response_status_code": None,
                "response_trace_id": "",
                "recorded_at": "",
                "response_payload": {},
            }
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO launchplane_idempotency_records "
                        "(record_id, scope, route_path, idempotency_key, request_fingerprint, "
                        "state, lease_owner, lease_expires_at, attempt, reconciliation_key, "
                        "created_at, updated_at, response_status_code, response_trace_id, "
                        "recorded_at, payload) VALUES "
                        "(:record_id, :scope, :route_path, :idempotency_key, "
                        ":request_fingerprint, 'reconcile_required', 'worker-a', '', 1, "
                        ":reconciliation_key, :created_at, :updated_at, NULL, '', '', "
                        "CAST(:payload AS jsonb))"
                    ),
                    {
                        "record_id": payload["record_id"],
                        "scope": payload["scope"],
                        "route_path": payload["route_path"],
                        "idempotency_key": payload["idempotency_key"],
                        "request_fingerprint": payload["request_fingerprint"],
                        "reconciliation_key": reconciliation_key,
                        "created_at": payload["created_at"],
                        "updated_at": payload["updated_at"],
                        "payload": json.dumps(payload),
                    },
                )
            engine.dispose()

            _upgrade_empty_database_to_head(database_url)
            store = PostgresRecordStore(database_url=database_url)
            try:
                store.verify_schema()
                loaded = store.read_idempotency_record(
                    scope=str(payload["scope"]),
                    route_path=str(payload["route_path"]),
                    idempotency_key=str(payload["idempotency_key"]),
                )
                blocked = store.reserve_mutation(
                    scope="github-actions:provider-target-migration",
                    route_path="/v1/test/provider-target",
                    idempotency_key="provider-target-contender",
                    request_fingerprint="provider-target-contender-fingerprint",
                    lease_owner="worker-b",
                    reconciliation_key=reconciliation_key,
                )
            finally:
                store.close()

        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.provider_target_key, reconciliation_key)
        self.assertEqual(blocked.status, "target_busy")

    def test_provider_target_fence_migration_fails_closed_on_duplicate_claims(self) -> None:
        with _isolated_postgres_database() as database_url:
            alembic_command.upgrade(_alembic_config(database_url), "a2b4c6d8e0f2")
            reconciliation_key = "dokploy:compose:duplicate-provider-target"
            engine = create_engine(database_url)
            with engine.begin() as connection:
                for index in (1, 2):
                    payload = {
                        "schema_version": 2,
                        "record_id": f"mutation-reservation-duplicate-target-{index}",
                        "scope": f"github-actions:duplicate-target-{index}",
                        "route_path": "/v1/test/provider-target",
                        "idempotency_key": f"provider-target-duplicate-{index}",
                        "request_fingerprint": f"provider-target-fingerprint-{index}",
                        "state": "reconcile_required",
                        "lease_owner": f"worker-{index}",
                        "lease_expires_at": "",
                        "attempt": 1,
                        "reconciliation_key": reconciliation_key,
                        "provider_effect_phase": "",
                        "provider_effect_started_at": "",
                        "created_at": "2026-07-12T00:00:00Z",
                        "updated_at": "2026-07-12T00:00:00Z",
                        "response_status_code": None,
                        "response_trace_id": "",
                        "recorded_at": "",
                        "response_payload": {},
                    }
                    connection.execute(
                        text(
                            "INSERT INTO launchplane_idempotency_records "
                            "(record_id, scope, route_path, idempotency_key, "
                            "request_fingerprint, state, lease_owner, lease_expires_at, "
                            "attempt, reconciliation_key, created_at, updated_at, "
                            "response_status_code, response_trace_id, recorded_at, payload) "
                            "VALUES (:record_id, :scope, :route_path, :idempotency_key, "
                            ":request_fingerprint, 'reconcile_required', :lease_owner, '', 1, "
                            ":reconciliation_key, :created_at, :updated_at, NULL, '', '', "
                            "CAST(:payload AS jsonb))"
                        ),
                        {
                            "record_id": payload["record_id"],
                            "scope": payload["scope"],
                            "route_path": payload["route_path"],
                            "idempotency_key": payload["idempotency_key"],
                            "request_fingerprint": payload["request_fingerprint"],
                            "lease_owner": payload["lease_owner"],
                            "reconciliation_key": reconciliation_key,
                            "created_at": payload["created_at"],
                            "updated_at": payload["updated_at"],
                            "payload": json.dumps(payload),
                        },
                    )
            engine.dispose()

            with self.assertRaisesRegex(
                RuntimeError,
                "Cannot fence active provider mutations while duplicate target claims exist",
            ):
                _upgrade_empty_database_to_head(database_url)

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

    def test_startup_verification_rejects_wrong_authz_active_predicate(self) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("drop index launchplane_authz_policies_active_uidx"))
                connection.execute(
                    text(
                        "create unique index launchplane_authz_policies_active_uidx "
                        "on launchplane_authz_policies (status) where status <> 'active'"
                    )
                )
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_authz_policies_active_uidx has predicate status<>'active'",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_fails_closed_when_outbox_claim_index_is_missing(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(text("drop index launchplane_outbox_deliveries_claim_idx"))
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_outbox_deliveries_claim_idx",
                ):
                    store.verify_schema()
            finally:
                store.close()

    def test_startup_verification_fails_closed_when_outbox_payload_is_not_jsonb(
        self,
    ) -> None:
        with _isolated_postgres_database() as database_url:
            _upgrade_empty_database_to_head(database_url)
            engine = create_engine(database_url)
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "alter table launchplane_outbox_deliveries "
                        "alter column payload type json using payload::json"
                    )
                )
            engine.dispose()
            store = PostgresRecordStore(database_url=database_url)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "launchplane_outbox_deliveries.payload has type",
                ):
                    store.verify_schema()
            finally:
                store.close()


class RealPostgresStorageConcurrencyTests(unittest.TestCase):
    def test_authz_policy_compare_write_serializes_concurrent_writers(self) -> None:
        with _store_for_fresh_head_database() as store:
            initial_record = store.seed_authz_policy_if_absent(
                LaunchplaneAuthzPolicyRecord(
                    record_id="seed",
                    source="test:initial",
                    updated_at="2026-07-18T00:00:00Z",
                    policy=LaunchplaneAuthzPolicy(),
                )
            )
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def reconcile(active_store: PostgresRecordStore, suffix: str) -> str:
                policy = LaunchplaneAuthzPolicy(
                    schema_version=2,
                    github_actions=(
                        GitHubActionsPolicyRule(
                            repository=f"example/{suffix}",
                            actions=("product_profile.read",),
                        ),
                    ),
                )
                replacement = LaunchplaneAuthzPolicyRecord(
                    record_id=build_authz_policy_record_id(
                        revision=2,
                        policy_sha256=authz_policy_sha256(policy),
                    ),
                    revision=2,
                    source="service:authz-managed-rule-set-reconcile",
                    updated_at=f"2026-07-18T00:00:0{suffix[-1]}Z",
                    policy=policy,
                )
                barrier.wait(timeout=5)
                return active_store.compare_and_write_authz_policy_record(
                    expected_record=initial_record,
                    replacement_record=replacement,
                    mutation=DbOnlyMutationRequest(
                        scope="github-actions:authz-concurrency",
                        route_path="/v1/authz-policies/managed-rule-sets/reconcile",
                        idempotency_key=f"authz:concurrent:{suffix}",
                        request_fingerprint=f"fingerprint-{suffix}",
                        lease_owner=f"trace-{suffix}",
                        response_status_code=202,
                        response_trace_id=f"trace-{suffix}",
                        response_payload={"status": "accepted", "suffix": suffix},
                    ),
                ).status

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: reconcile(*arguments),
                            ((store, "writer-1"), (second_store, "writer-2")),
                        )
                    )
                active_records = store.list_authz_policy_records(status="active")
                superseded_records = store.list_authz_policy_records(status="superseded")
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["stale", "written"])
        self.assertEqual(len(active_records), 1)
        self.assertEqual(active_records[0].revision, 2)
        self.assertEqual(
            superseded_records, (initial_record.model_copy(update={"status": "superseded"}),)
        )

    def test_concurrent_outbox_enqueue_reuses_one_delivery(self) -> None:
        with _store_for_fresh_head_database() as store:
            delivery = _outbox_delivery(suffix="concurrent-enqueue")
            second_store = PostgresRecordStore(database_url=store.database_url)
            barrier = threading.Barrier(2)

            def enqueue(active_store: PostgresRecordStore) -> OutboxDeliveryRecord:
                barrier.wait(timeout=5)
                return active_store.enqueue_outbox_delivery_with_idempotency(
                    OutboxWithIdempotencyRequest(delivery=delivery)
                )

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    first_future = executor.submit(enqueue, store)
                    second_future = executor.submit(enqueue, second_store)
                    first = first_future.result(timeout=10)
                    second = second_future.result(timeout=10)
                rows = store.list_outbox_delivery_records()
            finally:
                second_store.close()

        self.assertEqual(first.delivery_id, delivery.delivery_id)
        self.assertEqual(second.delivery_id, delivery.delivery_id)
        self.assertEqual([row.delivery_id for row in rows], [delivery.delivery_id])

    def test_lane_summary_waits_for_authority_bundle_commit(self) -> None:
        with _store_for_fresh_head_database() as store:
            bundle_step_reached = threading.Event()
            release_bundle = threading.Event()
            read_started = threading.Event()
            read_finished = threading.Event()
            errors: list[BaseException] = []

            class _BlockingStore(PostgresRecordStore):
                def _after_product_authority_bundle_step(self, step_name: str) -> None:
                    if step_name == "write_product_profile":
                        bundle_step_reached.set()
                        if not release_bundle.wait(timeout=5):
                            raise TimeoutError("timed out waiting to release authority bundle")

            blocking_store = _BlockingStore(database_url=store.database_url)
            reader_store = PostgresRecordStore(database_url=store.database_url)

            def write_bundle() -> None:
                try:
                    blocking_store.write_product_authority_bundle(
                        ProductAuthorityBundle(product_profiles=(_product_profile(),))
                    )
                except BaseException as exc:
                    errors.append(exc)

            def read_lane_summary() -> None:
                read_started.set()
                try:
                    reader_store.read_lane_summary(
                        context_name="example-product",
                        instance_name="prod",
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    read_finished.set()

            writer_thread = threading.Thread(target=write_bundle)
            reader_thread = threading.Thread(target=read_lane_summary)
            writer_thread.start()
            try:
                self.assertTrue(bundle_step_reached.wait(timeout=5))
                reader_thread.start()
                self.assertTrue(read_started.wait(timeout=5))
                self.assertFalse(read_finished.wait(timeout=0.1))
            finally:
                release_bundle.set()
                writer_thread.join(timeout=5)
                if reader_thread.ident is not None:
                    reader_thread.join(timeout=5)
                blocking_store.close()
                reader_store.close()

        self.assertFalse(writer_thread.is_alive())
        self.assertFalse(reader_thread.is_alive())
        self.assertEqual(errors, [])

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

    def test_outbox_claim_skips_locked_pending_row_and_claims_next_delivery(self) -> None:
        with _store_for_fresh_head_database() as store:
            first = _outbox_delivery(suffix="first").model_copy(
                update={
                    "created_at": "2026-07-13T00:00:00Z",
                    "updated_at": "2026-07-13T00:00:00Z",
                    "next_attempt_at": "2026-07-13T00:00:00Z",
                }
            )
            second = _outbox_delivery(suffix="second").model_copy(
                update={
                    "created_at": "2026-07-13T00:00:01Z",
                    "updated_at": "2026-07-13T00:00:01Z",
                    "next_attempt_at": "2026-07-13T00:00:00Z",
                }
            )
            store.write_outbox_delivery_record(first)
            store.write_outbox_delivery_record(second)
            blocker = create_engine(store.database_url)
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                with blocker.connect() as connection:
                    transaction = connection.begin()
                    try:
                        locked_delivery_id = connection.execute(
                            text(
                                "select delivery_id from launchplane_outbox_deliveries "
                                "where delivery_id = :delivery_id for update"
                            ),
                            {"delivery_id": first.delivery_id},
                        ).scalar_one()
                        claimed = second_store.claim_next_outbox_delivery_record(
                            lease_owner="worker-b",
                            now="2026-07-13T00:00:02Z",
                        )
                    finally:
                        transaction.rollback()
                first_claim = store.claim_next_outbox_delivery_record(
                    lease_owner="worker-a",
                    now="2026-07-13T00:00:03Z",
                )
            finally:
                second_store.close()
                blocker.dispose()

        self.assertEqual(locked_delivery_id, first.delivery_id)
        self.assertEqual(claimed.status, "claimed")
        assert claimed.record is not None
        self.assertEqual(claimed.record.delivery_id, second.delivery_id)
        self.assertEqual(claimed.record.lease_owner, "worker-b")
        self.assertEqual(first_claim.status, "claimed")
        assert first_claim.record is not None
        self.assertEqual(first_claim.record.delivery_id, first.delivery_id)
        self.assertEqual(first_claim.record.lease_owner, "worker-a")

    def test_outbox_enqueue_with_idempotency_is_atomic_on_validation_failure(self) -> None:
        with _store_for_fresh_head_database() as store:
            delivery = _outbox_delivery(suffix="atomic-validation")
            invalid_reservation = _mutation_reservation(
                lease_owner="worker-a",
                idempotency_key="product-preview-tls:postgres:outbox-invalid",
            )
            with self.assertRaisesRegex(ValueError, "completed replay evidence"):
                store.enqueue_outbox_delivery_with_idempotency(
                    OutboxWithIdempotencyRequest(
                        delivery=delivery,
                        idempotency_record=invalid_reservation,
                    )
                )
            rows = store.list_outbox_delivery_records()
            stored_reservation = store.read_idempotency_record(
                scope=invalid_reservation.scope,
                route_path=invalid_reservation.route_path,
                idempotency_key=invalid_reservation.idempotency_key,
            )

        self.assertEqual(rows, ())
        self.assertIsNone(stored_reservation)

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

    def test_merge_train_controller_lease_blocks_other_owner_until_expiry(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                first = store.acquire_merge_train_controller_state_record(
                    repository="cbusillo/sellyouroutboard",
                    base_branch="main",
                    policy_key="cbusillo/sellyouroutboard:main",
                    policy_sha256="policy-sha",
                    lease_owner="controller-a",
                    lease_seconds=30,
                )
                with self.assertRaisesRegex(
                    MergeTrainControllerLeaseHeldError,
                    "held by another owner",
                ):
                    second_store.acquire_merge_train_controller_state_record(
                        repository="cbusillo/sellyouroutboard",
                        base_branch="main",
                        policy_key="cbusillo/sellyouroutboard:main",
                        policy_sha256="policy-sha",
                        lease_owner="controller-b",
                        lease_seconds=30,
                    )
            finally:
                second_store.close()

        self.assertEqual(first.lease_owner, "controller-a")

    def test_merge_train_controller_takeover_fences_stale_owner(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                first = store.acquire_merge_train_controller_state_record(
                    repository="cbusillo/sellyouroutboard",
                    base_branch="main",
                    policy_key="cbusillo/sellyouroutboard:main",
                    policy_sha256="policy-sha",
                    lease_owner="controller-a",
                    lease_seconds=1,
                )
                time.sleep(1.1)
                renewed = second_store.acquire_merge_train_controller_state_record(
                    repository="cbusillo/sellyouroutboard",
                    base_branch="main",
                    policy_key="cbusillo/sellyouroutboard:main",
                    policy_sha256="policy-sha",
                    lease_owner="controller-b",
                    lease_seconds=30,
                )
                with self.assertRaisesRegex(
                    MergeTrainControllerLeaseLostError,
                    "lease owner changed",
                ):
                    store.compare_and_set_merge_train_controller_state_record(
                        record=first.model_copy(
                            update={
                                "active_action": "land_batch",
                                "active_phase": "merge_batch_entries",
                            }
                        ),
                        expected_lease_owner="controller-a",
                        expected_lease_acquired_at=first.lease_acquired_at,
                        lease_seconds=30,
                    )
                stored = second_store.read_merge_train_controller_state_record(
                    renewed.controller_key
                )
            finally:
                second_store.close()

        self.assertEqual(renewed.lease_owner, "controller-b")
        self.assertEqual(stored.lease_owner, "controller-b")

    def test_merge_train_controller_concurrent_acquire_has_one_owner(self) -> None:
        with _store_for_fresh_head_database() as store:
            barrier = threading.Barrier(2)

            def acquire(owner: str) -> MergeTrainControllerStateRecord | BaseException:
                contender = PostgresRecordStore(database_url=store.database_url)
                try:
                    barrier.wait(timeout=5)
                    return contender.acquire_merge_train_controller_state_record(
                        repository="cbusillo/sellyouroutboard",
                        base_branch="main",
                        policy_key="cbusillo/sellyouroutboard:main",
                        policy_sha256="policy-sha",
                        lease_owner=owner,
                        lease_seconds=30,
                    )
                except BaseException as error:  # noqa: BLE001
                    return error
                finally:
                    contender.close()

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(executor.map(acquire, ("controller-a", "controller-b")))
            winners = tuple(
                result for result in results if isinstance(result, MergeTrainControllerStateRecord)
            )
            conflicts = tuple(
                result
                for result in results
                if isinstance(result, MergeTrainControllerLeaseHeldError)
            )

        self.assertEqual(len(winners), 1)
        self.assertEqual(len(conflicts), 1)

    def test_merge_train_controller_expired_active_phase_is_adopted(self) -> None:
        with _store_for_fresh_head_database() as store:
            first = store.acquire_merge_train_controller_state_record(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                policy_key="cbusillo/sellyouroutboard:main",
                policy_sha256="policy-sha",
                lease_owner="controller-a",
                lease_seconds=1,
            )
            checkpointed = store.compare_and_set_merge_train_controller_state_record(
                record=first.model_copy(
                    update={
                        "active_action": "land_batch",
                        "active_phase": "cleanup_candidate_ref",
                        "active_record_id": "landing-record",
                        "step_payload": {"landing_plan_record_id": "landing-record"},
                    }
                ),
                expected_lease_owner=first.lease_owner,
                expected_lease_acquired_at=first.lease_acquired_at,
                lease_seconds=1,
            )
            time.sleep(1.1)
            adopted = store.acquire_merge_train_controller_state_record(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                policy_key="cbusillo/sellyouroutboard:main",
                policy_sha256="policy-sha",
                lease_owner="controller-b",
                lease_seconds=30,
            )

        self.assertEqual(checkpointed.active_phase, "cleanup_candidate_ref")
        self.assertEqual(adopted.lease_owner, "controller-b")
        self.assertEqual(adopted.reconciliation_status, "adopted")
        self.assertEqual(adopted.active_record_id, "landing-record")

    def test_merge_train_controller_operator_required_state_is_retried_by_service(self) -> None:
        with _store_for_fresh_head_database() as store:
            acquired = store.acquire_merge_train_controller_state_record(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                policy_key="cbusillo/sellyouroutboard:main",
                policy_sha256="policy-sha",
                lease_owner="controller-a",
                lease_seconds=30,
            )
            store.compare_and_set_merge_train_controller_state_record(
                record=acquired.model_copy(
                    update={
                        "status": "reconcile_required",
                        "lease_owner": "",
                        "lease_acquired_at": "",
                        "lease_expires_at": "",
                        "heartbeat_at": "",
                        "active_action": "execute_stack_collapse",
                        "active_phase": "merge_stack_branches",
                        "reconciliation_status": "required",
                        "reconciliation_detail": ("operator_required:github_request_rejected"),
                    }
                ),
                expected_lease_owner=acquired.lease_owner,
                expected_lease_acquired_at=acquired.lease_acquired_at,
                lease_seconds=30,
            )

            adopted = store.acquire_merge_train_controller_state_record(
                repository="cbusillo/sellyouroutboard",
                base_branch="main",
                policy_key="cbusillo/sellyouroutboard:main",
                policy_sha256="policy-sha",
                lease_owner="controller-b",
                lease_seconds=30,
            )

        self.assertEqual(adopted.lease_owner, "controller-b")
        self.assertEqual(adopted.reconciliation_status, "adopted")
        self.assertEqual(adopted.active_phase, "merge_stack_branches")
        self.assertIn("operator_required:github_request_rejected", adopted.reconciliation_detail)

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

    def test_route_binding_reconcile_serializes_distinct_keys_across_stores(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            record = _route_binding()
            first_mutation = DbOnlyMutationRequest(
                scope="github-actions:route-binding-test",
                route_path="/v1/route-bindings/reconcile",
                idempotency_key="route-binding-first",
                request_fingerprint="route-binding-fingerprint-first",
                lease_owner="worker-a",
                response_status_code=202,
                response_trace_id="trace-worker-a",
                response_payload={"status": "accepted", "trace_id": "trace-worker-a"},
            )
            second_mutation = DbOnlyMutationRequest(
                scope="github-actions:route-binding-test",
                route_path="/v1/route-bindings/reconcile",
                idempotency_key="route-binding-second",
                request_fingerprint="route-binding-fingerprint-second",
                lease_owner="worker-b",
                response_status_code=202,
                response_trace_id="trace-worker-b",
                response_payload={"status": "accepted", "trace_id": "trace-worker-b"},
            )
            barrier = threading.Barrier(2)

            def reconcile_binding(
                active_store: PostgresRecordStore,
                mutation: DbOnlyMutationRequest,
            ) -> str:
                barrier.wait()
                return active_store.reconcile_route_binding_record(
                    expected_record=None,
                    replacement_record=record,
                    mutation=mutation,
                ).status

            try:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = tuple(
                        executor.map(
                            lambda arguments: reconcile_binding(*arguments),
                            (
                                (store, first_mutation),
                                (second_store, second_mutation),
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
                    for reservation in (first_mutation, second_mutation)
                )
            finally:
                second_store.close()

        self.assertEqual(sorted(statuses), ["changed", "created"])
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


_PROVIDER_OPERATION_SCOPE = "github-actions:provider-operation-integration"
_PROVIDER_OPERATION_ROUTE = "/v1/drivers/provider-operation-integration"
_PROVIDER_OPERATION_KEY = "provider-operation:integration:1"
_PROVIDER_OPERATION_FINGERPRINT = "provider-operation-integration-fingerprint"
_PROVIDER_OPERATION_RECONCILIATION_KEY = "dokploy-compose:integration-preview"
_PROVIDER_OPERATION_TARGET_KEY = "dokploy-target:integration-preview"


class _IntegrationProviderAdapter:
    def __init__(
        self,
        *,
        started: threading.Event | None = None,
        release: threading.Event | None = None,
        observation: ProviderObservation | None = None,
    ) -> None:
        self._started = started
        self._release = release
        self._observation = observation or ProviderObservation(outcome="unknown")
        self.apply_calls = 0
        self.observe_calls = 0

    def reconciliation_key(self) -> str:
        return _PROVIDER_OPERATION_RECONCILIATION_KEY

    def target_key(self) -> str:
        return _PROVIDER_OPERATION_TARGET_KEY

    def observe(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
    ) -> ProviderObservation:
        del provider_operation_key, provider_effect_phase, reconciliation_key
        self.observe_calls += 1
        return self._observation

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        del provider_operation_key
        self.apply_calls += 1
        lease.checkpoint_effect("integration_effect")
        if self._started is not None:
            self._started.set()
        if self._release is not None and not self._release.wait(timeout=5):
            raise AssertionError("provider apply was not released")
        return ProviderMutationOutcome(
            response_status_code=202,
            response_payload={"trace_id": "integration-effect", "status": "accepted"},
        )


def _run_integration_provider_operation(
    store: PostgresRecordStore,
    adapter: _IntegrationProviderAdapter,
    *,
    lease_owner: str,
    response_trace_id: str,
    lease_seconds: int = 300,
    heartbeat_interval_seconds: float | None = None,
    idempotency_key: str = _PROVIDER_OPERATION_KEY,
    request_fingerprint: str = _PROVIDER_OPERATION_FINGERPRINT,
) -> DurableProviderOperationResult:
    return run_durable_provider_operation(
        store=store,
        scope=_PROVIDER_OPERATION_SCOPE,
        route_path=_PROVIDER_OPERATION_ROUTE,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        lease_owner=lease_owner,
        response_trace_id=response_trace_id,
        adapter=adapter,
        lease_seconds=lease_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )


class RealPostgresProviderOperationTests(unittest.TestCase):
    def test_two_instances_apply_provider_effect_exactly_once(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            try:
                started = threading.Event()
                release = threading.Event()
                holder = _IntegrationProviderAdapter(started=started, release=release)
                holder_result: dict[str, DurableProviderOperationResult] = {}

                def run_holder() -> None:
                    holder_result["value"] = _run_integration_provider_operation(
                        store,
                        holder,
                        lease_owner="instance-a",
                        response_trace_id="integration-trace-a",
                    )

                holder_thread = threading.Thread(target=run_holder)
                holder_thread.start()
                try:
                    self.assertTrue(started.wait(5))
                    second = _IntegrationProviderAdapter()
                    second_result = _run_integration_provider_operation(
                        second_store,
                        second,
                        lease_owner="instance-b",
                        response_trace_id="integration-trace-b",
                    )
                    self.assertEqual(second_result.status, "in_progress")
                    self.assertEqual(second.apply_calls, 0)
                finally:
                    release.set()
                    holder_thread.join(5)

                self.assertEqual(holder_result["value"].status, "completed")
                self.assertEqual(holder.apply_calls, 1)
                stored = store.read_idempotency_record(
                    scope=_PROVIDER_OPERATION_SCOPE,
                    route_path=_PROVIDER_OPERATION_ROUTE,
                    idempotency_key=_PROVIDER_OPERATION_KEY,
                )
                assert stored is not None
                self.assertEqual(stored.state, "completed")
            finally:
                second_store.close()

    def test_different_idempotency_key_cannot_bypass_active_target_fence(self) -> None:
        with _store_for_fresh_head_database() as store:
            first = store.reserve_mutation(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key="provider-operation:integration:first",
                request_fingerprint="provider-operation-integration-first",
                lease_owner="instance-a",
                lease_seconds=300,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
                provider_target_key=_PROVIDER_OPERATION_TARGET_KEY,
            )
            second = _run_integration_provider_operation(
                store,
                _IntegrationProviderAdapter(),
                lease_owner="instance-b",
                response_trace_id="integration-trace-b",
                idempotency_key="provider-operation:integration:second",
                request_fingerprint="provider-operation-integration-second",
            )

        self.assertEqual(first.status, "acquired")
        self.assertEqual(second.status, "target_busy")

    def test_heartbeats_hold_target_fence_past_original_lease(self) -> None:
        with _store_for_fresh_head_database() as store:
            second_store = PostgresRecordStore(database_url=store.database_url)
            started = threading.Event()
            release = threading.Event()
            holder = _IntegrationProviderAdapter(started=started, release=release)
            holder_result: dict[str, DurableProviderOperationResult] = {}

            def run_holder() -> None:
                holder_result["value"] = _run_integration_provider_operation(
                    store,
                    holder,
                    lease_owner="instance-a",
                    response_trace_id="integration-trace-a",
                    lease_seconds=2,
                    heartbeat_interval_seconds=0.2,
                )

            holder_thread = threading.Thread(target=run_holder)
            holder_thread.start()
            try:
                self.assertTrue(started.wait(5))
                time.sleep(2.2)
                second_result = _run_integration_provider_operation(
                    second_store,
                    _IntegrationProviderAdapter(),
                    lease_owner="instance-b",
                    response_trace_id="integration-trace-b",
                )
                self.assertEqual(second_result.status, "in_progress")
            finally:
                release.set()
                holder_thread.join(5)
                second_store.close()

            self.assertEqual(holder_result["value"].status, "completed")

    def test_restart_adopts_observed_effect_without_reapplying(self) -> None:
        with _store_for_fresh_head_database() as store:
            reservation = store.reserve_mutation(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key=_PROVIDER_OPERATION_KEY,
                request_fingerprint=_PROVIDER_OPERATION_FINGERPRINT,
                lease_owner="instance-a",
                lease_seconds=1,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
                provider_target_key=_PROVIDER_OPERATION_TARGET_KEY,
            )
            self.assertEqual(reservation.status, "acquired")
            time.sleep(1.2)

            recovery = _IntegrationProviderAdapter(
                observation=ProviderObservation(
                    outcome="present",
                    response_status_code=202,
                    response_payload={"trace_id": "adopted", "status": "accepted"},
                )
            )
            adopted = _run_integration_provider_operation(
                store,
                recovery,
                lease_owner="instance-b",
                response_trace_id="integration-trace-b",
            )

            self.assertEqual(adopted.status, "adopted")
            self.assertEqual(recovery.apply_calls, 0)
            stored = store.read_idempotency_record(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key=_PROVIDER_OPERATION_KEY,
            )
            assert stored is not None
            self.assertEqual(stored.state, "completed")

    def test_reconcile_required_fails_closed_when_effect_unknown(self) -> None:
        with _store_for_fresh_head_database() as store:
            reservation = store.reserve_mutation(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key=_PROVIDER_OPERATION_KEY,
                request_fingerprint=_PROVIDER_OPERATION_FINGERPRINT,
                lease_owner="instance-a",
                lease_seconds=300,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
                provider_target_key=_PROVIDER_OPERATION_TARGET_KEY,
            )
            store.mark_mutation_reconcile_required(
                reservation=reservation.record,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
            )

            recovery = _IntegrationProviderAdapter(
                observation=ProviderObservation(outcome="unknown")
            )
            result = _run_integration_provider_operation(
                store,
                recovery,
                lease_owner="instance-b",
                response_trace_id="integration-trace-b",
            )

            self.assertEqual(result.status, "reconcile_required")
            self.assertEqual(recovery.apply_calls, 0)
            stored = store.read_idempotency_record(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key=_PROVIDER_OPERATION_KEY,
            )
            assert stored is not None
            self.assertEqual(stored.state, "reconcile_required")

    def test_reconcile_absent_releases_and_reapplies_once(self) -> None:
        with _store_for_fresh_head_database() as store:
            reservation = store.reserve_mutation(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key=_PROVIDER_OPERATION_KEY,
                request_fingerprint=_PROVIDER_OPERATION_FINGERPRINT,
                lease_owner="instance-a",
                lease_seconds=300,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
                provider_target_key=_PROVIDER_OPERATION_TARGET_KEY,
            )
            store.mark_mutation_reconcile_required(
                reservation=reservation.record,
                reconciliation_key=_PROVIDER_OPERATION_RECONCILIATION_KEY,
            )
            recovery = _IntegrationProviderAdapter(
                observation=ProviderObservation(outcome="absent")
            )

            result = _run_integration_provider_operation(
                store,
                recovery,
                lease_owner="instance-b",
                response_trace_id="integration-trace-b",
            )

            self.assertEqual(result.status, "completed")
            self.assertEqual(recovery.observe_calls, 1)
            self.assertEqual(recovery.apply_calls, 1)
            stored = store.read_idempotency_record(
                scope=_PROVIDER_OPERATION_SCOPE,
                route_path=_PROVIDER_OPERATION_ROUTE,
                idempotency_key=_PROVIDER_OPERATION_KEY,
            )
            assert stored is not None
            self.assertEqual(stored.state, "completed")


if __name__ == "__main__":
    unittest.main()
