"""Native PostgreSQL compatibility-image startup and schema-transition fences."""

from concurrent.futures import ThreadPoolExecutor
from functools import partial
import os
import threading
import unittest
from unittest.mock import patch

from alembic import command
from sqlalchemy import (
    Connection,
    Engine,
    create_engine,
    event,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, PublicHostEffect
from control_plane.contracts.product_profile_record import ProductLaneProfile
from control_plane.storage.migrations.versions import (
    e7c1d5e9a3b6_drop_retired_delivery_tables as held,
    e8d2f6a0b4c7_drop_preview_enablement as previous_release,
    e9e3a7b1c5d8_join_retirement_paths as previous_join,
    f0a4c8e2b6d9_canonical_public_host_fences as hostnames,
    f1b5d9a3c7e0_join_public_host_fences as hostname_join,
)
from control_plane.storage.migrations.versions.f0a4c8e2b6d9_canonical_public_host_fences import (
    PUBLIC_HOST_SCHEMA_TRANSITION_LOCK_KEY,
)
from control_plane.storage.postgres import LaunchplaneDokployTargetRow, PostgresRecordStore
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    PublicHostAuthorityBusy,
)
from control_plane.storage.schema_migration import (
    alembic_config,
    migrate_schema,
)
from control_plane.storage.schema_invariants import (
    AUTHZ_COMPATIBILITY_FLOOR_REVISION,
    PRE_RETIREMENT_ALEMBIC_REVISION,
)
from tests.test_retired_delivery_migration import history_snapshot, seed_retired_history
from tests.test_postgres_integration import (
    _isolated_postgres_database,
    _product_profile,
    _store_for_fresh_head_database,
)


@unittest.skipUnless(os.environ.get("LAUNCHPLANE_TEST_POSTGRES_URL"), "PostgreSQL URL required")
class SchemaCompatibilityPostgresTests(unittest.TestCase):
    def test_compatibility_startup_and_image_rollback_preserve_old_and_future_schemas(
        self,
    ) -> None:
        for revision, expected in (
            ("", previous_release.revision),
            (AUTHZ_COMPATIBILITY_FLOOR_REVISION, previous_release.revision),
            (PRE_RETIREMENT_ALEMBIC_REVISION, previous_release.revision),
            (previous_release.revision, previous_release.revision),
            (held.revision, previous_join.revision),
            (previous_join.revision, previous_join.revision),
            (hostnames.revision, hostnames.revision),
            (hostname_join.revision, hostname_join.revision),
        ):
            with self.subTest(revision=revision), _isolated_postgres_database() as url:
                if revision:
                    command.upgrade(alembic_config(url), revision)
                engine = create_engine(url)
                self.addCleanup(engine.dispose)
                before_tables = set(inspect(engine).get_table_names())
                retains_history = set(held.RETIRED_TABLES).issubset(before_tables)
                # The earlier floor does not yet have every historical table.
                retained_history = ""
                if retains_history:
                    seed_retired_history(engine)
                    retained_history = history_snapshot(engine, held.RETIRED_TABLES)
                store = PostgresRecordStore(database_url=url)
                self.addCleanup(store.close)
                target_payload = None
                if revision not in ("", AUTHZ_COMPATIBILITY_FLOOR_REVISION):
                    self._seed_public_target(store)
                    with engine.connect() as connection:
                        target_payload = connection.scalar(
                            select(LaunchplaneDokployTargetRow.payload)
                        )

                self.assertEqual(migrate_schema(database_url=url), expected)
                store.verify_schema()
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.scalar(text("SELECT version_num FROM alembic_version")),
                        expected,
                    )
                    if target_payload is not None:
                        self.assertEqual(
                            connection.scalar(select(LaunchplaneDokployTargetRow.payload)),
                            target_payload,
                        )
                after_tables = set(inspect(engine).get_table_names())
                if revision in (
                    "",
                    AUTHZ_COMPATIBILITY_FLOOR_REVISION,
                    PRE_RETIREMENT_ALEMBIC_REVISION,
                    previous_release.revision,
                    hostnames.revision,
                ):
                    self.assertTrue(set(held.RETIRED_TABLES).issubset(after_tables))
                if retains_history:
                    self.assertTrue(set(held.RETIRED_TABLES).issubset(after_tables))
                    self.assertEqual(
                        history_snapshot(engine, held.RETIRED_TABLES), retained_history
                    )
                if revision in (held.revision, previous_join.revision, hostname_join.revision):
                    self.assertTrue(set(held.RETIRED_TABLES).isdisjoint(after_tables))

                # Future graph knowledge never authorizes joining the held drop.
                if revision in (hostnames.revision, hostname_join.revision):
                    for target in (held.revision, previous_join.revision):
                        if revision == hostnames.revision:
                            with self.assertRaises(ValueError):
                                migrate_schema(database_url=url, target_revision=target)
                            self.assertEqual(
                                history_snapshot(engine, held.RETIRED_TABLES), retained_history
                            )
                        else:
                            self.assertEqual(
                                migrate_schema(database_url=url, target_revision=target), revision
                            )
                        with engine.connect() as connection:
                            self.assertEqual(
                                connection.scalar(text("SELECT version_num FROM alembic_version")),
                                revision,
                            )

    def test_explicit_delivery_request_keeps_its_historical_join(self) -> None:
        with _isolated_postgres_database() as url:
            migrate_schema(database_url=url)
            self.assertEqual(
                migrate_schema(database_url=url, target_revision=held.revision),
                previous_join.revision,
            )
            store = PostgresRecordStore(database_url=url)
            self.addCleanup(store.close)
            store.verify_schema()

    def _seed_public_target(
        self, store: PostgresRecordStore, *, context: str = "public-site"
    ) -> tuple[DokployTargetRecord, DokployTargetIdRecord, ProviderTargetRecord]:
        profile = _product_profile().model_copy(
            update={
                "product": context,
                "lanes": (ProductLaneProfile(context=context, instance="prod"),),
            }
        )
        store.write_product_profile_record(profile)
        target = DokployTargetRecord(
            context=context,
            instance="prod",
            domains=(f"origin.{context}.example",),
            public_hosts=(f"public.{context}.example",),
            updated_at=profile.updated_at,
        )
        target_id = DokployTargetIdRecord(
            context=context,
            instance="prod",
            target_id=f"{context}-compose",
            updated_at=target.updated_at,
        )
        provider = ProviderTargetRecord.from_dokploy_records(
            target_record=target, target_id_record=target_id
        )
        store.write_dokploy_target_record(target)
        store.write_dokploy_target_id_record(target_id)
        store.write_provider_target_record(provider)
        return target, target_id, provider

    def test_migration_refuses_before_first_key_and_during_provider_io_in_both_directions(
        self,
    ) -> None:
        for direction in ("upgrade", "downgrade"):
            for phase in ("before_key", "provider_io"):
                with (
                    self.subTest(direction=direction, phase=phase),
                    _isolated_postgres_database() as url,
                ):
                    command.upgrade(alembic_config(url), hostnames.revision)
                    store = PostgresRecordStore(database_url=url)
                    self.addCleanup(store.close)
                    target, target_id, provider = self._seed_public_target(store)
                    if direction == "upgrade":
                        command.downgrade(alembic_config(url), previous_release.revision)
                    starting_revision = (
                        previous_release.revision if direction == "upgrade" else hostnames.revision
                    )
                    with store._engine.connect() as connection:
                        starting_payload = connection.scalar(
                            select(LaunchplaneDokployTargetRow.payload)
                        )
                    entered, release = threading.Event(), threading.Event()
                    exclusive_at_entry: list[bool] = []

                    def pause() -> None:
                        entered.set()
                        if not release.wait(15):
                            raise TimeoutError("Migration writer fixture was not released")

                    def before_key(
                        connection: Connection,
                        _cursor: object,
                        _statement: str,
                        _parameters: object,
                        _context: object,
                        _executemany: bool,
                    ) -> None:
                        if phase != "before_key" or entered.is_set():
                            return
                        # Observe the real backend lock before its next statement;
                        # a separate DBAPI cursor preserves the pending cursor and
                        # avoids recursively invoking SQLAlchemy's event hooks.
                        driver = connection.connection.driver_connection
                        assert driver is not None
                        probe = driver.cursor()
                        try:
                            probe.execute(
                                "SELECT bool_or(mode = 'ShareLock' AND objsubid = 1 AND "
                                "((classid::bigint << 32) | objid::bigint) = "
                                "hashtextextended(%s, 0)), bool_or(mode = 'ExclusiveLock') "
                                "FROM pg_locks WHERE pid = pg_backend_pid() "
                                "AND granted AND locktype = 'advisory'",
                                (PUBLIC_HOST_SCHEMA_TRANSITION_LOCK_KEY,),
                            )
                            observation = probe.fetchone()
                        finally:
                            probe.close()
                        if observation is not None and observation[0]:
                            exclusive_at_entry.append(bool(observation[1]))
                            pause()

                    def apply(_checkpoint: object) -> None:
                        if phase == "provider_io":
                            pause()

                    def migrate() -> None:
                        if direction == "upgrade":
                            command.upgrade(alembic_config(url), hostnames.revision)
                        else:
                            command.downgrade(alembic_config(url), previous_release.revision)

                    event.listen(store._engine, "before_cursor_execute", before_key)
                    try:
                        with ThreadPoolExecutor(max_workers=1) as workers:
                            applying = workers.submit(
                                store.write_product_public_hosts_bundle,
                                ProductAuthorityBundle(dokploy_targets=(target,)),
                                expected_target=target,
                                expected_target_id=target_id,
                                expected_provider_target=provider,
                                apply_provider=apply,
                            )
                            try:
                                self.assertTrue(entered.wait(10))
                                if phase == "before_key":
                                    self.assertTrue(exclusive_at_entry)
                                    self.assertFalse(any(exclusive_at_entry))
                                with self.assertRaises(RuntimeError):
                                    migrate()
                                with store._engine.connect() as connection:
                                    self.assertEqual(
                                        connection.scalar(
                                            text("SELECT version_num FROM alembic_version")
                                        ),
                                        starting_revision,
                                    )
                                    self.assertEqual(
                                        connection.scalar(
                                            select(LaunchplaneDokployTargetRow.payload)
                                        ),
                                        starting_payload,
                                    )
                            finally:
                                release.set()
                            applying.result(timeout=10)
                    finally:
                        event.remove(store._engine, "before_cursor_execute", before_key)
                    migrate()

    def test_migration_refuses_uncommitted_target_insert_without_runtime_mutex(self) -> None:
        for direction in ("upgrade", "downgrade"):
            with self.subTest(direction=direction), _isolated_postgres_database() as url:
                command.upgrade(alembic_config(url), hostnames.revision)
                engine = create_engine(url)
                self.addCleanup(engine.dispose)
                if direction == "upgrade":
                    command.downgrade(alembic_config(url), previous_release.revision)

                def migrate() -> None:
                    if direction == "upgrade":
                        command.upgrade(alembic_config(url), hostnames.revision)
                    else:
                        command.downgrade(alembic_config(url), previous_release.revision)

                target = DokployTargetRecord(
                    context="uncommitted",
                    instance="testing",
                    domains=("public.example",),
                    updated_at="2026-10-20T15:00:00Z",
                )
                with engine.begin() as connection:
                    connection.execute(
                        insert(LaunchplaneDokployTargetRow).values(
                            context=target.context,
                            instance=target.instance,
                            updated_at=target.updated_at,
                            payload=target.model_dump(mode="json"),
                        )
                    )
                    with self.assertRaises(DBAPIError) as refusal:
                        migrate()
                    self.assertEqual(getattr(refusal.exception.orig, "sqlstate", None), "55P03")
                # A refused migration leaves no transition mutex or partial DDL.
                migrate()
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.scalar(select(LaunchplaneDokployTargetRow.payload)),
                        target.model_dump(mode="json"),
                    )

    def test_independent_public_host_operations_can_both_enter_provider_io(self) -> None:
        with _store_for_fresh_head_database() as store:
            other = PostgresRecordStore(database_url=store.database_url)
            self.addCleanup(other.close)
            first = self._seed_public_target(store, context="first")
            second = self._seed_public_target(other, context="second")
            release = threading.Event()
            entered = (threading.Event(), threading.Event())

            def apply(index: int, _checkpoint: object) -> None:
                entered[index].set()
                if not release.wait(15):
                    raise TimeoutError("Independent provider fixture was not released")

            with ThreadPoolExecutor(max_workers=2) as workers:
                futures = []
                try:
                    for index, (writer, records) in enumerate(((store, first), (other, second))):
                        target, target_id, provider = records
                        futures.append(
                            workers.submit(
                                writer.write_product_public_hosts_bundle,
                                ProductAuthorityBundle(dokploy_targets=(target,)),
                                expected_target=target,
                                expected_target_id=target_id,
                                expected_provider_target=provider,
                                apply_provider=partial(apply, index),
                            )
                        )
                        self.assertTrue(entered[index].wait(10))
                finally:
                    release.set()
                for future in futures:
                    future.result(timeout=10)

    def test_checkpoint_survives_migration_winning_reacquisition_gap(self) -> None:
        with _isolated_postgres_database() as url:
            command.upgrade(alembic_config(url), hostnames.revision)
            store = PostgresRecordStore(database_url=url)
            self.addCleanup(store.close)
            target, target_id, provider = self._seed_public_target(store)
            pending = PublicHostEffect(
                host=target.public_hosts[0],
                provider_host="provider.example",
                target_id=target_id.target_id,
                runtime_port=8069,
            )
            in_provider = threading.Event()
            committed, migrating, release_migration = (
                threading.Event(),
                threading.Event(),
                threading.Event(),
            )
            next_effect = threading.Event()
            original_commit = Session.commit

            def commit(session: Session) -> None:
                original_commit(session)
                if in_provider.is_set() and not committed.is_set():
                    committed.set()
                    if not migrating.wait(10):
                        raise TimeoutError("Migration did not enter checkpoint gap")

            def replace_keys(
                _connection: object,
                _cursor: object,
                statement: str,
                _parameters: object,
                _context: object,
                _executemany: bool,
            ) -> None:
                if (
                    "CREATE OR REPLACE FUNCTION launchplane_public_host_authority_keys("
                    in statement
                ):
                    migrating.set()
                    if not release_migration.wait(15):
                        raise TimeoutError("Checkpoint migration fixture was not released")

            def apply(checkpoint: object) -> None:
                from collections.abc import Callable
                from typing import cast

                in_provider.set()
                cast(Callable[[str, str, int], None], checkpoint)(
                    pending.host, "saved-domain", 8069
                )
                next_effect.set()

            event.listen(Engine, "before_cursor_execute", replace_keys)
            self.addCleanup(event.remove, Engine, "before_cursor_execute", replace_keys)
            with (
                patch.object(Session, "commit", commit),
                ThreadPoolExecutor(max_workers=2) as workers,
            ):
                applying = workers.submit(
                    store.write_product_public_hosts_bundle,
                    ProductAuthorityBundle(dokploy_targets=(target,)),
                    expected_target=target,
                    expected_target_id=target_id,
                    expected_provider_target=provider,
                    pending_effects=(pending,),
                    apply_provider=apply,
                )
                migration = None
                try:
                    self.assertTrue(committed.wait(10))
                    migration = workers.submit(
                        command.downgrade, alembic_config(url), previous_release.revision
                    )
                    self.assertTrue(migrating.wait(10))
                    with self.assertRaises(PublicHostAuthorityBusy):
                        applying.result(timeout=10)
                    self.assertFalse(next_effect.is_set())
                    saved = store.read_dokploy_target_record(
                        context_name=target.context, instance_name=target.instance
                    )
                    self.assertEqual(
                        saved.pending_public_host_effects,
                        (pending.model_copy(update={"domain_id": "saved-domain"}),),
                    )
                finally:
                    release_migration.set()
                if migration is not None:
                    migration.result(timeout=10)
