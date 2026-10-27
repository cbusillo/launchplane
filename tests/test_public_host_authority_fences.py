from concurrent.futures import ThreadPoolExecutor
import os
import threading
import unittest

from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import (
    create_engine,
    insert,
    inspect,
    select,
    text,
    update,
)

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, PublicHostEffect
from control_plane.contracts.product_profile_record import ProductLaneProfile
from control_plane.storage.migrations.versions import (
    f0a4c8e2b6d9_canonical_public_host_fences as hostnames,
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
            with (
                engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                hostnames.downgrade()
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
            with (
                engine.begin() as connection,
                Operations.context(MigrationContext.configure(connection)),
            ):
                hostnames.upgrade()
            check_fences()
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
                    EXPECTED_ALEMBIC_HEAD_REVISION if initial_revision else target_revision,
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
            writer.write_dokploy_target_record(
                unrelated.model_copy(update={"domains": ("PUBLIC.EXAMPLE.",)})
            )

    def test_private_endpoint_writers_cannot_cross_provider_io_and_keep_cleanup(self) -> None:
        from control_plane.contracts.private_health_endpoint_record import (
            PrivateHealthEndpointRecord,
        )

        with _store_for_fresh_head_database() as store:
            target, target_id, provider = self._seed_public_target(store)
            writer = PostgresRecordStore(database_url=store.database_url)
            self.addCleanup(writer.close)
            disabled = PrivateHealthEndpointRecord(
                endpoint_key="foreign",
                product="foreign",
                context="foreign",
                instance="testing",
                url=f"https://{target.public_hosts[0]}",
                status="disabled",
                updated_at=target.updated_at,
            )
            writer.write_private_health_endpoint_record(disabled)
            entered, release, effected = threading.Event(), threading.Event(), threading.Event()

            def apply(_checkpoint: object) -> None:
                entered.set()
                if not release.wait(15):
                    raise TimeoutError("Provider fixture was not released")
                effected.set()

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
                    for key in ("foreign", "new-endpoint"):
                        active = disabled.model_copy(
                            update={"endpoint_key": key, "status": "active"}
                        )
                        with self.subTest(key=key), self.assertRaises(PublicHostAuthorityBusy):
                            workers.submit(
                                writer.write_private_health_endpoint_record, active
                            ).result(timeout=3)
                    self.assertEqual(
                        writer.read_private_health_endpoint_record("foreign"), disabled
                    )
                    writer.write_private_health_endpoint_record(disabled)
                    for key in ("first-monitor", "second-monitor"):
                        writer.write_private_health_endpoint_record(
                            disabled.model_copy(
                                update={
                                    "endpoint_key": key,
                                    "status": "active",
                                    "url": "http://shared.internal:8069/health",
                                }
                            )
                        )
                    unrelated = target.model_copy(
                        update={
                            "context": "unrelated",
                            "domains": ("other.example",),
                            "public_hosts": (),
                        }
                    )
                    workers.submit(writer.write_dokploy_target_record, unrelated).result(timeout=3)
                finally:
                    release.set()
                applying.result(timeout=10)
            self.assertTrue(effected.is_set())

    def test_endpoint_first_refuses_public_effect_before_provider_and_disable_allows_retry(
        self,
    ) -> None:
        from control_plane.contracts.private_health_endpoint_record import (
            PrivateHealthEndpointRecord,
        )
        from control_plane.contracts.dokploy_target_record import DokployTargetRecordChanged

        with _store_for_fresh_head_database() as store:
            target, target_id, provider = self._seed_public_target(store)
            target = target.model_copy(update={"public_hosts": (), "domains": ("origin.example",)})
            store.write_dokploy_target_record(target)
            endpoint = PrivateHealthEndpointRecord(
                endpoint_key="foreign",
                product="foreign",
                context="foreign",
                instance="testing",
                url="https://public.example",
                updated_at=target.updated_at,
            )
            store.write_private_health_endpoint_record(endpoint)
            requested = target.model_copy(update={"public_hosts": ("public.example",)})
            effected = threading.Event()

            def apply(_checkpoint: object) -> None:
                effected.set()

            def publish() -> None:
                store.write_product_public_hosts_bundle(
                    ProductAuthorityBundle(dokploy_targets=(requested,)),
                    expected_target=target,
                    expected_target_id=target_id,
                    expected_provider_target=provider,
                    apply_provider=apply,
                )

            with self.assertRaises(DokployTargetRecordChanged):
                publish()
            self.assertFalse(effected.is_set())
            store.write_private_health_endpoint_record(
                endpoint.model_copy(update={"status": "disabled"})
            )
            publish()
            self.assertTrue(effected.is_set())
            with self.assertRaises(DokployTargetRecordChanged):
                store.write_private_health_endpoint_record(endpoint)
            self.assertEqual(
                store.read_private_health_endpoint_record("foreign").status, "disabled"
            )
