import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pydantic import BaseModel

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
)
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentDeleteEvent,
    RuntimeEnvironmentRecord,
)
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretVersion,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProviderTargetWrite,
    RuntimeEnvironmentDelete,
)


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


class _FailingPostgresRecordStore(PostgresRecordStore):
    def __init__(self, *, database_url: str, fail_step: str | None = None) -> None:
        super().__init__(database_url=database_url)
        self.fail_step = fail_step
        self.steps: list[str] = []

    def _after_product_authority_bundle_step(self, step_name: str) -> None:
        self.steps.append(step_name)
        if step_name == self.fail_step:
            raise RuntimeError(f"injected failure after {step_name}")


class _FailingFilesystemRecordStore(FilesystemRecordStore):
    def __init__(self, state_dir: Path, *, fail_step: str | None = None) -> None:
        super().__init__(state_dir)
        self.fail_step = fail_step
        self.steps: list[str] = []

    def _after_product_authority_bundle_step(self, step_name: str) -> None:
        self.steps.append(step_name)
        if step_name == self.fail_step:
            raise RuntimeError(f"injected failure after {step_name}")


class _BlockingFilesystemRecordStore(FilesystemRecordStore):
    def __init__(self, state_dir: Path) -> None:
        super().__init__(state_dir)
        self.block_next_provider_target_write = False
        self.write_started = threading.Event()
        self.release_write = threading.Event()

    def _write_model_locked(self, record_type: str, record_id: str, model: BaseModel) -> Path:
        if self.block_next_provider_target_write and record_type == "launchplane_provider_targets":
            self.block_next_provider_target_write = False
            self.write_started.set()
            if not self.release_write.wait(timeout=5):
                raise TimeoutError("timed out waiting to release provider-target write")
        return super()._write_model_locked(record_type, record_id, model)


def _product_profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="example-product",
        display_name="Example Product",
        repository="example/example-product",
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/example/example-product"),
        runtime_port=8080,
        health_path="/health",
        updated_at="2026-07-01T00:00:00Z",
        source="test:authority-bundle",
    )


def _dokploy_target(
    *, context: str = "example-product", instance: str = "prod"
) -> DokployTargetRecord:
    return DokployTargetRecord(
        context=context,
        instance=instance,
        target_type="application",
        target_name=f"{context}-{instance}",
        updated_at="2026-07-01T00:00:00Z",
        source_label="test:authority-bundle",
    )


def _dokploy_target_id(
    *, context: str = "example-product", instance: str = "prod", target_id: str = "app-prod"
) -> DokployTargetIdRecord:
    return DokployTargetIdRecord(
        context=context,
        instance=instance,
        target_id=target_id,
        updated_at="2026-07-01T00:00:01Z",
        source_label="test:authority-bundle",
    )


def _provider_target(
    *, context: str = "example-product", instance: str = "prod", target_id: str = "app-prod"
) -> ProviderTargetRecord:
    return ProviderTargetRecord(
        context=context,
        instance=instance,
        provider_id="dokploy",
        target_category="application",
        target_id=target_id,
        display_name=f"{context}-{instance}",
        provider_target_type="application",
        updated_at="2026-07-01T00:00:02Z",
        source_label="test:authority-bundle",
    )


def _runtime_environment(
    *, context: str = "example-product", instance: str = "prod"
) -> RuntimeEnvironmentRecord:
    return RuntimeEnvironmentRecord(
        scope="instance",
        context=context,
        instance=instance,
        env={"EXAMPLE_MODE": "enabled"},
        updated_at="2026-07-01T00:00:03Z",
        source_label="test:authority-bundle",
    )


def _runtime_delete_event(record: RuntimeEnvironmentRecord) -> RuntimeEnvironmentDeleteEvent:
    return RuntimeEnvironmentDeleteEvent(
        event_id=f"runtime-delete-{record.context}-{record.instance}",
        recorded_at="2026-07-01T00:00:04Z",
        actor="test-suite",
        scope=record.scope,
        context=record.context,
        instance=record.instance,
        source_label="test:authority-bundle",
        env_keys=tuple(sorted(record.env)),
        env_value_count=len(record.env),
        detail="Injected cleanup delete event.",
    )


def _secret_version() -> SecretVersion:
    return SecretVersion(
        version_id="secret-example-v1",
        secret_id="secret-example",
        created_at="2026-07-01T00:00:05Z",
        created_by="test-suite",
        ciphertext="gAAAAABexampleciphertext",
    )


def _secret_record() -> SecretRecord:
    return SecretRecord(
        secret_id="secret-example",
        scope="context_instance",
        integration="runtime_environment",
        name="EXAMPLE_SECRET",
        context="example-product",
        instance="prod",
        current_version_id="secret-example-v1",
        created_at="2026-07-01T00:00:05Z",
        updated_at="2026-07-01T00:00:05Z",
        updated_by="test-suite",
    )


def _secret_binding() -> SecretBinding:
    return SecretBinding(
        binding_id="secret-binding-example",
        secret_id="secret-example",
        integration="runtime_environment",
        binding_key="EXAMPLE_SECRET",
        context="example-product",
        instance="prod",
        created_at="2026-07-01T00:00:06Z",
        updated_at="2026-07-01T00:00:06Z",
    )


def _secret_audit_event() -> SecretAuditEvent:
    return SecretAuditEvent(
        event_id="secret-audit-example-created",
        secret_id="secret-example",
        event_type="created",
        recorded_at="2026-07-01T00:00:07Z",
        actor="test-suite",
        detail="Created by authority bundle test.",
    )


def _environment_inventory() -> EnvironmentInventory:
    return EnvironmentInventory(
        context="example-product",
        instance="prod",
        source_git_ref="refs/heads/main",
        deploy=DeploymentEvidence(
            target_name="example-product-prod",
            target_type="application",
            deploy_mode="deploy",
            status="pass",
        ),
        updated_at="2026-07-01T00:00:08Z",
        deployment_record_id="deployment-example-prod",
    )


def _release_tuple() -> ReleaseTupleRecord:
    return ReleaseTupleRecord(
        tuple_id="tuple-example-prod",
        context="example-product",
        channel="prod",
        artifact_id="artifact-example-prod",
        repo_shas={"example-product": "abcdef1"},
        deployment_record_id="deployment-example-prod",
        provenance="ship",
        minted_at="2026-07-01T00:00:09Z",
    )


def _idempotency_record() -> LaunchplaneIdempotencyRecord:
    return LaunchplaneIdempotencyRecord(
        record_id="idempotency-trace-authority-bundle",
        scope="test-suite",
        route_path="/v1/test/authority-bundle/apply",
        idempotency_key="authority-bundle-key",
        request_fingerprint="authority-bundle-fingerprint",
        response_status_code=202,
        response_trace_id="trace-authority-bundle",
        recorded_at="2026-07-01T00:00:10Z",
        response_payload={"trace_id": "trace-authority-bundle", "result": {"status": "ok"}},
    )


def _write_bundle() -> ProductAuthorityBundle:
    return ProductAuthorityBundle(
        product_profiles=(_product_profile(),),
        dokploy_targets=(_dokploy_target(),),
        dokploy_target_ids=(_dokploy_target_id(),),
        provider_target_writes=(
            ProviderTargetWrite(record=_provider_target(), expected_absent=True),
        ),
        runtime_environments=(_runtime_environment(),),
        secret_versions=(_secret_version(),),
        secret_records=(_secret_record(),),
        secret_bindings=(_secret_binding(),),
        secret_audit_events=(_secret_audit_event(),),
        environment_inventory=(_environment_inventory(),),
        release_tuples=(_release_tuple(),),
        idempotency_record=_idempotency_record(),
    )


def _delete_bundle() -> ProductAuthorityBundle:
    runtime_record = _runtime_environment(context="example-legacy", instance="old-prod")
    return ProductAuthorityBundle(
        delete_runtime_environments=(
            RuntimeEnvironmentDelete(
                expected_record=runtime_record,
                event=_runtime_delete_event(runtime_record),
            ),
        ),
        delete_provider_targets=(
            _provider_target(context="example-legacy", instance="old-prod", target_id="old-app"),
        ),
        delete_dokploy_target_ids=(
            _dokploy_target_id(context="example-legacy", instance="old-prod", target_id="old-app"),
        ),
        delete_dokploy_targets=(_dokploy_target(context="example-legacy", instance="old-prod"),),
        idempotency_record=_idempotency_record().model_copy(
            update={"record_id": "idempotency-trace-authority-cleanup"}
        ),
    )


def _seed_cleanup_records(
    store: PostgresRecordStore | FilesystemRecordStore,
) -> RuntimeEnvironmentRecord:
    runtime_record = _runtime_environment(context="example-legacy", instance="old-prod")
    store.write_runtime_environment_record(runtime_record)
    store.write_provider_target_record(
        _provider_target(context="example-legacy", instance="old-prod", target_id="old-app")
    )
    store.write_dokploy_target_id_record(
        _dokploy_target_id(context="example-legacy", instance="old-prod", target_id="old-app")
    )
    store.write_dokploy_target_record(
        _dokploy_target(context="example-legacy", instance="old-prod")
    )
    return runtime_record


def _assert_cleanup_records_present(
    test_case: unittest.TestCase,
    store: PostgresRecordStore | FilesystemRecordStore,
    runtime_record: RuntimeEnvironmentRecord,
) -> None:
    test_case.assertEqual(
        store.list_runtime_environment_records(
            context_name="example-legacy", instance_name="old-prod"
        ),
        (runtime_record,),
    )
    test_case.assertEqual(store.list_runtime_environment_delete_events(), ())
    test_case.assertEqual(
        store.read_provider_target_record(
            context_name="example-legacy", instance_name="old-prod"
        ).target_id,
        "old-app",
    )
    test_case.assertEqual(
        store.read_dokploy_target_id_record(
            context_name="example-legacy", instance_name="old-prod"
        ).target_id,
        "old-app",
    )
    test_case.assertIsNone(
        store.read_idempotency_record(
            scope="test-suite",
            route_path="/v1/test/authority-bundle/apply",
            idempotency_key="authority-bundle-key",
        )
    )


def _assert_cleanup_records_removed(
    test_case: unittest.TestCase,
    store: PostgresRecordStore | FilesystemRecordStore,
) -> None:
    test_case.assertEqual(
        store.list_runtime_environment_records(
            context_name="example-legacy", instance_name="old-prod"
        ),
        (),
    )
    test_case.assertEqual(len(store.list_runtime_environment_delete_events()), 1)
    test_case.assertEqual(store.list_provider_target_records(), ())
    test_case.assertEqual(store.list_dokploy_target_id_records(), ())
    test_case.assertEqual(store.list_dokploy_target_records(), ())
    test_case.assertIsNotNone(
        store.read_idempotency_record(
            scope="test-suite",
            route_path="/v1/test/authority-bundle/apply",
            idempotency_key="authority-bundle-key",
        )
    )


def _assert_write_bundle_published(
    test_case: unittest.TestCase,
    store: PostgresRecordStore | FilesystemRecordStore,
) -> None:
    test_case.assertEqual(len(store.list_product_profile_records()), 1)
    test_case.assertEqual(len(store.list_dokploy_target_records()), 1)
    test_case.assertEqual(len(store.list_dokploy_target_id_records()), 1)
    test_case.assertEqual(len(store.list_provider_target_records()), 1)
    test_case.assertEqual(len(store.list_runtime_environment_records()), 1)
    test_case.assertEqual(len(store.list_secret_records()), 1)
    test_case.assertEqual(len(store.list_secret_versions(secret_id="secret-example")), 1)
    test_case.assertEqual(len(store.list_secret_bindings()), 1)
    test_case.assertEqual(len(store.list_secret_audit_events(secret_id="secret-example")), 1)
    test_case.assertEqual(len(store.list_environment_inventory()), 1)
    test_case.assertEqual(len(store.list_release_tuple_records()), 1)
    test_case.assertIsNotNone(
        store.read_idempotency_record(
            scope="test-suite",
            route_path="/v1/test/authority-bundle/apply",
            idempotency_key="authority-bundle-key",
        )
    )


def _assert_write_bundle_absent(
    test_case: unittest.TestCase,
    store: PostgresRecordStore | FilesystemRecordStore,
) -> None:
    test_case.assertEqual(store.list_product_profile_records(), ())
    test_case.assertEqual(store.list_dokploy_target_records(), ())
    test_case.assertEqual(store.list_dokploy_target_id_records(), ())
    test_case.assertEqual(store.list_provider_target_records(), ())
    test_case.assertEqual(store.list_runtime_environment_records(), ())
    test_case.assertEqual(store.list_secret_versions(secret_id="secret-example"), ())
    test_case.assertEqual(store.list_secret_records(), ())
    test_case.assertEqual(store.list_secret_bindings(), ())
    test_case.assertEqual(store.list_secret_audit_events(secret_id="secret-example"), ())
    test_case.assertEqual(store.list_environment_inventory(), ())
    test_case.assertEqual(store.list_release_tuple_records(), ())
    test_case.assertIsNone(
        store.read_idempotency_record(
            scope="test-suite",
            route_path="/v1/test/authority-bundle/apply",
            idempotency_key="authority-bundle-key",
        )
    )


class ProductAuthorityBundleTransactionTests(unittest.TestCase):
    def test_postgres_authority_bundle_rejects_stale_provider_target_absence(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            store = _FailingPostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_dir) / "launchplane.sqlite3")
            )
            store.ensure_schema()
            concurrent_target = _provider_target(target_id="concurrent-app")
            store.write_provider_target_record(concurrent_target)

            with self.assertRaisesRegex(
                ValueError,
                "created after authority bundle planning",
            ):
                store.write_product_authority_bundle(_write_bundle())

            self.assertEqual(store.list_provider_target_records(), (concurrent_target,))
            self.assertEqual(store.list_product_profile_records(), ())
            self.assertEqual(store.list_runtime_environment_records(), ())
            self.assertIsNone(
                store.read_idempotency_record(
                    scope="test-suite",
                    route_path="/v1/test/authority-bundle/apply",
                    idempotency_key="authority-bundle-key",
                )
            )

    def test_filesystem_authority_bundle_rejects_stale_provider_target_absence(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            state_dir = Path(temporary_dir)
            store = FilesystemRecordStore(state_dir)
            concurrent_target = _provider_target(target_id="concurrent-app")
            store.write_provider_target_record(concurrent_target)

            with self.assertRaisesRegex(
                ValueError,
                "created after authority bundle planning",
            ):
                store.write_product_authority_bundle(_write_bundle())

            self.assertEqual(store.list_provider_target_records(), (concurrent_target,))
            self.assertEqual(store.list_product_profile_records(), ())
            self.assertEqual(store.list_runtime_environment_records(), ())
            self.assertIsNone(
                store.read_idempotency_record(
                    scope="test-suite",
                    route_path="/v1/test/authority-bundle/apply",
                    idempotency_key="authority-bundle-key",
                )
            )
            self.assertFalse((state_dir / ".product_authority_bundle_stages").exists())

    def test_postgres_authority_bundle_rolls_back_after_each_write_step(self) -> None:
        write_steps = (
            "write_product_profile",
            "write_dokploy_target",
            "write_dokploy_target_id",
            "write_provider_target",
            "write_runtime_environment",
            "write_secret_version",
            "write_secret_record",
            "write_secret_binding",
            "write_secret_audit_event",
            "write_environment_inventory",
            "write_release_tuple",
            "write_idempotency",
        )
        for fail_step in write_steps:
            with self.subTest(fail_step=fail_step), TemporaryDirectory() as temporary_dir:
                store = _FailingPostgresRecordStore(
                    database_url=_sqlite_database_url(Path(temporary_dir) / "launchplane.sqlite3"),
                    fail_step=fail_step,
                )
                store.ensure_schema()

                with self.assertRaisesRegex(RuntimeError, fail_step):
                    store.write_product_authority_bundle(_write_bundle())

                self.assertIn(fail_step, store.steps)
                self.assertEqual(store.list_product_profile_records(), ())
                self.assertEqual(store.list_dokploy_target_records(), ())
                self.assertEqual(store.list_dokploy_target_id_records(), ())
                self.assertEqual(store.list_provider_target_records(), ())
                self.assertEqual(store.list_runtime_environment_records(), ())
                self.assertEqual(store.list_secret_versions(secret_id="secret-example"), ())
                self.assertEqual(store.list_secret_records(), ())
                self.assertEqual(store.list_secret_bindings(), ())
                self.assertEqual(store.list_secret_audit_events(secret_id="secret-example"), ())
                self.assertEqual(store.list_environment_inventory(), ())
                self.assertEqual(store.list_release_tuple_records(), ())
                self.assertIsNone(
                    store.read_idempotency_record(
                        scope="test-suite",
                        route_path="/v1/test/authority-bundle/apply",
                        idempotency_key="authority-bundle-key",
                    )
                )

    def test_postgres_authority_bundle_commits_full_graph_and_idempotency(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            store = _FailingPostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_dir) / "launchplane.sqlite3")
            )
            store.ensure_schema()

            store.write_product_authority_bundle(_write_bundle())

            self.assertEqual(len(store.list_product_profile_records()), 1)
            self.assertEqual(len(store.list_dokploy_target_records()), 1)
            self.assertEqual(len(store.list_provider_target_records()), 1)
            self.assertEqual(len(store.list_runtime_environment_records()), 1)
            self.assertEqual(len(store.list_secret_versions(secret_id="secret-example")), 1)
            self.assertEqual(len(store.list_secret_records()), 1)
            self.assertEqual(len(store.list_secret_bindings()), 1)
            self.assertEqual(len(store.list_secret_audit_events(secret_id="secret-example")), 1)
            self.assertEqual(len(store.list_environment_inventory()), 1)
            self.assertEqual(len(store.list_release_tuple_records()), 1)
            self.assertIsNotNone(
                store.read_idempotency_record(
                    scope="test-suite",
                    route_path="/v1/test/authority-bundle/apply",
                    idempotency_key="authority-bundle-key",
                )
            )

    def test_postgres_authority_bundle_rolls_back_cleanup_deletes_and_audit(self) -> None:
        delete_steps = (
            "delete_runtime_environment",
            "write_runtime_environment_delete_event",
            "delete_provider_target",
            "delete_dokploy_target_id",
            "delete_dokploy_target",
            "write_idempotency",
        )
        for fail_step in delete_steps:
            with self.subTest(fail_step=fail_step), TemporaryDirectory() as temporary_dir:
                store = _FailingPostgresRecordStore(
                    database_url=_sqlite_database_url(Path(temporary_dir) / "launchplane.sqlite3"),
                    fail_step=fail_step,
                )
                store.ensure_schema()
                runtime_record = _seed_cleanup_records(store)

                with self.assertRaisesRegex(RuntimeError, fail_step):
                    store.write_product_authority_bundle(_delete_bundle())

                self.assertIn(fail_step, store.steps)
                _assert_cleanup_records_present(self, store, runtime_record)

    def test_filesystem_staging_failures_discard_unpublished_bundle(self) -> None:
        stage_steps = (
            "stage_write_product_profile",
            "stage_write_dokploy_target",
            "stage_write_dokploy_target_id",
            "stage_write_provider_target",
            "stage_write_runtime_environment",
            "stage_write_secret_version",
            "stage_write_secret_record",
            "stage_write_secret_binding",
            "stage_write_secret_audit_event",
            "stage_write_environment_inventory",
            "stage_write_release_tuple",
            "stage_write_idempotency",
            "stage_product_authority_bundle",
        )
        for fail_step in stage_steps:
            with self.subTest(fail_step=fail_step), TemporaryDirectory() as temporary_dir:
                state_dir = Path(temporary_dir)
                store = _FailingFilesystemRecordStore(state_dir, fail_step=fail_step)

                with self.assertRaisesRegex(RuntimeError, fail_step):
                    store.write_product_authority_bundle(_write_bundle())

                _assert_write_bundle_absent(self, FilesystemRecordStore(state_dir))
                self.assertFalse((state_dir / ".product_authority_bundle_stages").exists())

    def test_filesystem_ready_stage_recovers_by_discarding_unpublished_bundle(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            state_dir = Path(temporary_dir)
            store = _FailingFilesystemRecordStore(
                state_dir,
                fail_step="stage_product_authority_bundle",
            )

            with self.assertRaisesRegex(RuntimeError, "stage_product_authority_bundle"):
                store.write_product_authority_bundle(_write_bundle())

            self.assertEqual(FilesystemRecordStore(state_dir).list_product_profile_records(), ())
            self.assertFalse((state_dir / ".product_authority_bundle_stages").exists())

    def test_filesystem_publishing_stage_recovers_after_each_write_step(self) -> None:
        publish_steps = (
            "publish_product_authority_bundle",
            "write_product_profile",
            "write_dokploy_target",
            "write_dokploy_target_id",
            "write_provider_target",
            "write_runtime_environment",
            "write_secret_version",
            "write_secret_record",
            "write_secret_binding",
            "write_secret_audit_event",
            "write_environment_inventory",
            "write_release_tuple",
            "write_idempotency",
        )
        for fail_step in publish_steps:
            with self.subTest(fail_step=fail_step), TemporaryDirectory() as temporary_dir:
                state_dir = Path(temporary_dir)
                store = _FailingFilesystemRecordStore(state_dir, fail_step=fail_step)

                with self.assertRaisesRegex(RuntimeError, fail_step):
                    store.write_product_authority_bundle(_write_bundle())

                recovered_store = FilesystemRecordStore(state_dir)
                _assert_write_bundle_published(self, recovered_store)
                self.assertFalse((state_dir / ".product_authority_bundle_stages").exists())

    def test_filesystem_cleanup_staging_failures_discard_without_live_deletes(self) -> None:
        stage_steps = (
            "stage_delete_runtime_environment",
            "stage_write_runtime_environment_delete_event",
            "stage_delete_provider_target",
            "stage_delete_dokploy_target_id",
            "stage_delete_dokploy_target",
            "stage_write_idempotency",
            "stage_product_authority_bundle",
        )
        for fail_step in stage_steps:
            with self.subTest(fail_step=fail_step), TemporaryDirectory() as temporary_dir:
                state_dir = Path(temporary_dir)
                seed_store = FilesystemRecordStore(state_dir)
                runtime_record = _seed_cleanup_records(seed_store)
                store = _FailingFilesystemRecordStore(state_dir, fail_step=fail_step)

                with self.assertRaisesRegex(RuntimeError, fail_step):
                    store.write_product_authority_bundle(_delete_bundle())

                recovered_store = FilesystemRecordStore(state_dir)
                _assert_cleanup_records_present(self, recovered_store, runtime_record)
                self.assertFalse((state_dir / ".product_authority_bundle_stages").exists())

    def test_filesystem_cleanup_publishing_stage_recovers_after_each_step(self) -> None:
        publish_steps = (
            "publish_product_authority_bundle",
            "delete_runtime_environment",
            "write_runtime_environment_delete_event",
            "delete_provider_target",
            "delete_dokploy_target_id",
            "delete_dokploy_target",
            "write_idempotency",
        )
        for fail_step in publish_steps:
            with self.subTest(fail_step=fail_step), TemporaryDirectory() as temporary_dir:
                state_dir = Path(temporary_dir)
                _seed_cleanup_records(FilesystemRecordStore(state_dir))
                store = _FailingFilesystemRecordStore(state_dir, fail_step=fail_step)

                with self.assertRaisesRegex(RuntimeError, fail_step):
                    store.write_product_authority_bundle(_delete_bundle())

                recovered_store = FilesystemRecordStore(state_dir)
                _assert_cleanup_records_removed(self, recovered_store)
                self.assertFalse((state_dir / ".product_authority_bundle_stages").exists())

    def test_filesystem_bundle_serializes_with_ordinary_record_write(self) -> None:
        with TemporaryDirectory() as temporary_dir:
            state_dir = Path(temporary_dir)
            store = _BlockingFilesystemRecordStore(state_dir)
            runtime_record = _seed_cleanup_records(store)
            replacement_target = _provider_target(
                context="example-legacy",
                instance="old-prod",
                target_id="replacement-app",
            )
            write_errors: list[BaseException] = []
            bundle_errors: list[BaseException] = []
            bundle_started = threading.Event()
            bundle_finished = threading.Event()

            def write_target() -> None:
                try:
                    store.write_provider_target_record(replacement_target)
                except BaseException as exc:
                    write_errors.append(exc)

            def delete_bundle() -> None:
                bundle_started.set()
                try:
                    store.write_product_authority_bundle(_delete_bundle())
                except BaseException as exc:
                    bundle_errors.append(exc)
                finally:
                    bundle_finished.set()

            store.block_next_provider_target_write = True
            write_thread = threading.Thread(target=write_target)
            bundle_thread = threading.Thread(target=delete_bundle)
            write_thread.start()
            try:
                self.assertTrue(store.write_started.wait(timeout=5))
                bundle_thread.start()
                self.assertTrue(bundle_started.wait(timeout=5))
                self.assertFalse(bundle_finished.wait(timeout=0.1))
            finally:
                store.release_write.set()
                write_thread.join(timeout=5)
                if bundle_thread.ident is not None:
                    bundle_thread.join(timeout=5)

            self.assertFalse(write_thread.is_alive())
            self.assertFalse(bundle_thread.is_alive())
            self.assertEqual(write_errors, [])
            self.assertEqual(len(bundle_errors), 1)
            self.assertIsInstance(bundle_errors[0], ValueError)
            self.assertEqual(
                store.read_provider_target_record(
                    context_name="example-legacy",
                    instance_name="old-prod",
                ),
                replacement_target,
            )
            self.assertEqual(
                store.list_runtime_environment_records(
                    context_name="example-legacy",
                    instance_name="old-prod",
                ),
                (runtime_record,),
            )
            self.assertEqual(store.list_runtime_environment_delete_events(), ())
            self.assertEqual(
                store.read_dokploy_target_id_record(
                    context_name="example-legacy",
                    instance_name="old-prod",
                ).target_id,
                "old-app",
            )
            self.assertIsNone(
                store.read_idempotency_record(
                    scope="test-suite",
                    route_path="/v1/test/authority-bundle/apply",
                    idempotency_key="authority-bundle-key",
                )
            )


if __name__ == "__main__":
    unittest.main()
