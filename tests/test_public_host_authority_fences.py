"""Native PostgreSQL DNS identities, retained rows and provider-I/O fences."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import threading
import unittest

from alembic import command
from sqlalchemy import create_engine, insert, inspect, select, text, update

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, PublicHostEffect
from control_plane.contracts.product_profile_record import ProductLaneProfile
from control_plane.storage.migrations.versions import (
    e7c1d5e9a3b6_drop_retired_delivery_tables as held,
    e8d2f6a0b4c7_drop_preview_enablement as previous_release,
    e9e3a7b1c5d8_join_retirement_paths as previous_join,
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
    def test_existing_alias_rows_gain_canonical_fences_without_rewriting_payloads(self) -> None:
        with _isolated_postgres_database() as url:
            config = alembic_config(url)
            command.upgrade(config, previous_release.revision)
            engine = create_engine(url)
            self.addCleanup(engine.dispose)
            seed_retired_history(engine)
            before = history_snapshot(engine, held.RETIRED_TABLES)
            canonical = {"context": "retained", "domains": ["public.example"]}
            aliases = (
                "PUBLIC.EXAMPLE",
                "public.example.",
                " \tPUBLIC.EXAMPLE.\r\n",
                "\u00a0PUBLIC.EXAMPLE.\u2003",
            )
            retained = DokployTargetRecord(
                context="retained",
                instance="testing",
                domains=aliases,
                updated_at="2026-10-20T15:00:00Z",
            )
            query = text(
                "SELECT launchplane_public_host_authority_keys("
                "'launchplane_dokploy_targets', CAST(:payload AS jsonb))"
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
                original_keys = set(
                    connection.execute(query, {"payload": retained.model_dump_json()}).scalars()
                )
            old_store = PostgresRecordStore(database_url=url)
            try:
                with self.assertRaises(RuntimeError):
                    old_store.verify_schema()
            finally:
                old_store.close()
            self.assertEqual(migrate_schema(database_url=url), SCHEMA_MIGRATION_TARGET_REVISION)
            self.assertEqual(history_snapshot(engine, held.RETIRED_TABLES), before)
            with engine.connect() as connection:
                original = connection.execute(
                    select(LaunchplaneDokployTargetRow.payload)
                ).scalar_one()
                self.assertEqual(original, retained.model_dump(mode="json"))
                expected = set(
                    connection.execute(query, {"payload": json.dumps(canonical)}).scalars()
                )
                for field in ("domains", "public_hosts", "pending_public_host_effects"):
                    for alias in aliases:
                        with self.subTest(field=field, alias=alias):
                            payload = {
                                "context": canonical["context"],
                                field: [{"host": alias}]
                                if field == "pending_public_host_effects"
                                else [alias],
                            }
                            self.assertEqual(
                                set(
                                    connection.execute(
                                        query, {"payload": json.dumps(payload)}
                                    ).scalars()
                                ),
                                expected,
                            )
            command.downgrade(config, previous_release.revision)
            with engine.connect() as connection:
                self.assertEqual(
                    set(
                        connection.execute(query, {"payload": retained.model_dump_json()}).scalars()
                    ),
                    original_keys,
                )
                self.assertEqual(
                    connection.execute(select(LaunchplaneDokployTargetRow.payload)).scalar_one(),
                    retained.model_dump(mode="json"),
                )
            self.assertEqual(migrate_schema(database_url=url), SCHEMA_MIGRATION_TARGET_REVISION)
            writer = PostgresRecordStore(database_url=url)
            self.addCleanup(writer.close)
            with engine.begin() as connection:
                connection.execute(
                    update(LaunchplaneDokployTargetRow).values(
                        payload=retained.model_dump(mode="json")
                    )
                )
                with self.assertRaises(PublicHostAuthorityBusy):
                    writer.write_dokploy_target_record(
                        retained.model_copy(
                            update={"context": "foreign", "domains": ("public.example",)}
                        )
                    )
            writer.write_dokploy_target_record(
                retained.model_copy(update={"context": "foreign", "domains": ("public.example",)})
            )

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
        for initial_revision in ("", SCHEMA_MIGRATION_TARGET_REVISION):
            with (
                self.subTest(initial_revision=initial_revision),
                _isolated_postgres_database() as url,
            ):
                if initial_revision:
                    command.upgrade(alembic_config(url), initial_revision)
                self.assertEqual(
                    migrate_schema(database_url=url, target_revision=held.revision),
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
