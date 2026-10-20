"""Native PostgreSQL DNS identities, retained rows and provider-I/O fences."""

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
    update,
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
    SCHEMA_MIGRATION_TARGET_REVISION,
    alembic_config,
    migrate_schema,
)
from control_plane.storage.schema_invariants import EXPECTED_ALEMBIC_HEAD_REVISION
from tests.test_retired_delivery_migration import history_snapshot, seed_retired_history
from tests.test_postgres_integration import (
    _isolated_postgres_database,
    _product_profile,
    _store_for_fresh_head_database,
)


@unittest.skipUnless(os.environ.get("LAUNCHPLANE_TEST_POSTGRES_URL"), "PostgreSQL URL required")
class PublicHostAuthorityPostgresTests(unittest.TestCase):
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
                    migrate_schema(database_url=url)
                    store = PostgresRecordStore(database_url=url)
                    self.addCleanup(store.close)
                    target, target_id, provider = self._seed_public_target(store)
                    if direction == "upgrade":
                        command.downgrade(alembic_config(url), previous_release.revision)
                    starting_revision = (
                        previous_release.revision
                        if direction == "upgrade"
                        else SCHEMA_MIGRATION_TARGET_REVISION
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
                            migrate_schema(database_url=url)
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
                                with self.assertRaisesRegex(RuntimeError, "busy"):
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
                migrate_schema(database_url=url)
                engine = create_engine(url)
                self.addCleanup(engine.dispose)
                if direction == "upgrade":
                    command.downgrade(alembic_config(url), previous_release.revision)

                def migrate() -> None:
                    if direction == "upgrade":
                        migrate_schema(database_url=url)
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
            migrate_schema(database_url=url)
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

    def test_existing_alias_rows_gain_canonical_fences_without_rewriting_payloads(self) -> None:
        with _isolated_postgres_database() as url:
            config = alembic_config(url)
            command.upgrade(config, previous_release.revision)
            engine = create_engine(url)
            self.addCleanup(engine.dispose)
            seed_retired_history(engine)
            before = history_snapshot(engine, held.RETIRED_TABLES)
            aliases = (
                "PUBLIC.EXAMPLE.",
                " \tPUBLIC.EXAMPLE.\r\n",
                "\u00a0PUBLIC.EXAMPLE.\u2003",
                "https://PUBLIC.EXAMPLE./path",
                "https://public.example:8443/path?query#fragment",
            )
            retained = DokployTargetRecord(
                context="retained",
                instance="testing",
                domains=aliases,
                updated_at="2026-10-20T15:00:00Z",
            )
            with engine.begin() as connection:
                connection.execute(
                    insert(LaunchplaneDokployTargetRow).values(
                        context=retained.context,
                        instance=retained.instance,
                        updated_at=retained.updated_at,
                        payload=retained.model_dump(mode="json"),
                    )
                )
            old_store = PostgresRecordStore(database_url=url)
            try:
                with self.assertRaises(RuntimeError):
                    old_store.verify_schema()
            finally:
                old_store.close()
            writer = PostgresRecordStore(database_url=url)
            self.addCleanup(writer.close)
            foreign = retained.model_copy(
                update={"context": "foreign", "domains": ("public.example",)}
            )

            def check_fences() -> None:
                for field in ("domains", "public_hosts", "pending_public_host_effects"):
                    for alias in aliases:
                        with self.subTest(field=field, alias=alias), engine.connect() as connection:
                            payload = retained.model_dump(mode="json")
                            payload.update(
                                domains=[], public_hosts=[], pending_public_host_effects=[]
                            )
                            payload[field] = (
                                [
                                    PublicHostEffect(
                                        host=alias,
                                        provider_host="provider.example",
                                        target_id="retained-compose",
                                        runtime_port=8069,
                                    ).model_dump(mode="json")
                                ]
                                if field == "pending_public_host_effects"
                                else [alias]
                            )
                            transaction = connection.begin()
                            try:
                                connection.execute(
                                    update(LaunchplaneDokployTargetRow)
                                    .where(
                                        LaunchplaneDokployTargetRow.context == retained.context,
                                    )
                                    .values(payload=payload)
                                )
                                with self.assertRaises(PublicHostAuthorityBusy):
                                    writer.write_dokploy_target_record(foreign)
                            finally:
                                transaction.rollback()
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.execute(
                            select(LaunchplaneDokployTargetRow.payload)
                        ).scalar_one(),
                        retained.model_dump(mode="json"),
                    )
                self.assertEqual(history_snapshot(engine, held.RETIRED_TABLES), before)

            self.assertEqual(migrate_schema(database_url=url), SCHEMA_MIGRATION_TARGET_REVISION)
            check_fences()
            command.downgrade(config, previous_release.revision)
            # A URL alias has a distinct old fence. This proves actual reversal,
            # without depending on the internal key-function name or key layout.
            with engine.begin() as connection:
                connection.execute(
                    update(LaunchplaneDokployTargetRow).values(
                        payload=retained.model_copy(update={"domains": (aliases[-1],)}).model_dump(
                            mode="json"
                        )
                    )
                )
                writer.write_dokploy_target_record(foreign)
            writer.delete_dokploy_target_record(expected_record=foreign)
            with engine.begin() as connection:
                connection.execute(
                    update(LaunchplaneDokployTargetRow).values(
                        payload=retained.model_dump(mode="json")
                    )
                )
            self.assertEqual(migrate_schema(database_url=url), SCHEMA_MIGRATION_TARGET_REVISION)
            check_fences()
            # Settled transactions no longer exclude a legitimate next writer.
            writer.write_dokploy_target_record(foreign)

    def test_default_migration_preserves_held_drop_across_existing_schema_states(self) -> None:
        for revision, delivery_already_applied in (
            (held.down_revision, False),
            (previous_release.revision, False),
            (held.revision, True),
            (previous_join.revision, True),
            (SCHEMA_MIGRATION_TARGET_REVISION, False),
            (EXPECTED_ALEMBIC_HEAD_REVISION, True),
        ):
            with self.subTest(revision=revision), _isolated_postgres_database() as url:
                command.upgrade(alembic_config(url), revision)
                expected = (
                    EXPECTED_ALEMBIC_HEAD_REVISION
                    if delivery_already_applied
                    else SCHEMA_MIGRATION_TARGET_REVISION
                )
                self.assertEqual(migrate_schema(database_url=url), expected)
                engine = create_engine(url)
                try:
                    present = set(inspect(engine).get_table_names())
                    if delivery_already_applied:
                        self.assertTrue(set(held.RETIRED_TABLES).isdisjoint(present))
                    else:
                        self.assertTrue(set(held.RETIRED_TABLES).issubset(present))
                    with engine.connect() as connection:
                        self.assertEqual(
                            connection.execute(text("SELECT version_num FROM alembic_version"))
                            .scalars()
                            .all(),
                            [expected],
                        )
                    store = PostgresRecordStore(database_url=url)
                    try:
                        store.verify_schema()
                    finally:
                        store.close()
                finally:
                    engine.dispose()

    def test_explicit_held_target_joins_after_the_normal_hostname_upgrade(self) -> None:
        for target_revision in (held.revision, previous_join.revision):
            self._check_explicit_retirement_target(target_revision)

    def _check_explicit_retirement_target(self, target_revision: str) -> None:
        for initial_revision in ("", SCHEMA_MIGRATION_TARGET_REVISION):
            with (
                self.subTest(initial_revision=initial_revision),
                _isolated_postgres_database() as url,
            ):
                if initial_revision:
                    command.upgrade(alembic_config(url), initial_revision)
                self.assertEqual(
                    migrate_schema(database_url=url, target_revision=target_revision),
                    EXPECTED_ALEMBIC_HEAD_REVISION,
                )
                self.assertEqual(migrate_schema(database_url=url), EXPECTED_ALEMBIC_HEAD_REVISION)

    def test_equivalent_foreign_writers_share_fence_during_provider_io(self) -> None:
        with _store_for_fresh_head_database() as store:
            profile = _product_profile().model_copy(
                update={"lanes": (ProductLaneProfile(context="public-site", instance="prod"),)}
            )
            store.write_product_profile_record(profile)
            target = DokployTargetRecord(
                context=profile.lanes[0].context,
                instance=profile.lanes[0].instance,
                domains=("origin.example",),
                public_hosts=("public.example",),
                updated_at=profile.updated_at,
            )
            target_id = DokployTargetIdRecord(
                context=target.context,
                instance=target.instance,
                target_id="public-compose",
                updated_at=target.updated_at,
            )
            provider = ProviderTargetRecord.from_dokploy_records(
                target_record=target, target_id_record=target_id
            )
            store.write_dokploy_target_record(target)
            store.write_dokploy_target_id_record(target_id)
            store.write_provider_target_record(provider)
            writer = PostgresRecordStore(database_url=store.database_url)
            self.addCleanup(writer.close)
            entered, release = threading.Event(), threading.Event()

            def apply(_checkpoint: object) -> None:
                entered.set()
                if not release.wait(10):
                    raise TimeoutError("Provider fixture was not released")

            with ThreadPoolExecutor(max_workers=2) as workers:
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
                    # An unrelated writer can commit while provider I/O is held.
                    unrelated = target.model_copy(
                        update={
                            "context": "unrelated",
                            "domains": ("distinct.example",),
                            "public_hosts": (),
                        }
                    )
                    workers.submit(writer.write_dokploy_target_record, unrelated).result(timeout=2)
                    for alias in (
                        "PUBLIC.EXAMPLE",
                        "public.example.",
                        " \tPUBLIC.EXAMPLE.\n",
                        "\u00a0PUBLIC.EXAMPLE.\u2003",
                        "https://PUBLIC.EXAMPLE./path",
                        "https://public.example:8443/path?query#fragment",
                    ):
                        for field in ("domains", "pending_public_host_effects"):
                            with self.subTest(alias=alias, field=field):
                                other = unrelated.model_copy(
                                    update={
                                        "domains": (alias,) if field == "domains" else (),
                                        "pending_public_host_effects": (
                                            PublicHostEffect(
                                                host=alias,
                                                provider_host="public.example",
                                                target_id="foreign-compose",
                                                runtime_port=8069,
                                            ),
                                        )
                                        if field == "pending_public_host_effects"
                                        else (),
                                    }
                                )
                                with self.assertRaises(PublicHostAuthorityBusy):
                                    workers.submit(
                                        writer.write_dokploy_target_record, other
                                    ).result(timeout=2)
                    self.assertEqual(
                        writer.read_dokploy_target_record(
                            context_name=unrelated.context, instance_name=unrelated.instance
                        ),
                        unrelated,
                    )
                finally:
                    release.set()
                applying.result(timeout=10)
            # The transaction ended, so the retained alias can now acquire its fence.
            writer.write_dokploy_target_record(
                unrelated.model_copy(update={"domains": ("PUBLIC.EXAMPLE.",)})
            )
