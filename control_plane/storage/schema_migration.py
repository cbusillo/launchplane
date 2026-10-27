from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
from typing import Literal

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

from control_plane.storage.postgres import _build_engine, PostgresRecordStore
from control_plane.storage.migrations.versions import (
    e7c1d5e9a3b6_drop_retired_delivery_tables as retired_delivery,
    e8d2f6a0b4c7_drop_preview_enablement as retired_preview,
    e9e3a7b1c5d8_join_retirement_paths as retirement_join,
    b81ca763d924_add_discussion_topics as discussion_topics,
    da72490fce13_join_topics_retirement as topics_join,
    f0a4c8e2b6d9_canonical_public_host_fences as public_host_fences,
    f1b5d9a3c7e0_join_public_host_fences as public_host_join,
    f2c6e0a4b8d1_join_topics_public_host_fences as topics_hostnames,
    f3d7a1b5c9e2_join_topics_hostname_retirement as compatibility_join,
    a4c8e0f2b6d3_archive_nonruntime_product_profiles as profile_archive,
    a5d9f1b3c7e4_join_archive_topics_retirement as archive_retirement_join,
    a6e0a2c4d8f5_join_archive_topics_hostnames as archive_hostnames_join,
    a7f1b3d5e9a6_join_archive_compatibility as archive_compatibility_join,
    b8f2c4d6e0a7_managed_preview_settings as preview_settings,
    b9a3d5e7f1b8_join_preview_retirement as preview_retirement_join,
    c0b4e6f8a2c9_join_preview_hostnames as preview_hostnames_join,
    c1c5f7a9b3d0_join_preview_compatibility as preview_compatibility_join,
    b0a2c4e6f8d1_retire_preview_idle_reservations as idle_retirement,
    b1b3d5f7a9e2_join_idle_retirement as idle_retirement_join,
    b2c4e6a8f0d3_join_idle_hostnames as idle_hostnames_join,
    f87c72d690ab_join_idle_compatibility as idle_compatibility_join,
    c8d2e4f6a9b1_add_integration_attestations as integration_attestations,
    d1a5b7c9d2e4_join_attestations_compatibility as attestations_compatibility_join,
)
from control_plane.storage.schema_adoption import schema_stamp_revision_for_engine
from control_plane.storage.schema_invariants import (
    AUTHZ_COMPATIBILITY_FLOOR_REVISION,
    PRE_RETIREMENT_ALEMBIC_REVISION,
)

SCHEMA_MIGRATION_TARGET_REVISION = idle_hostnames_join.revision
SCHEMA_MIGRATION_SUPPORTED_REVISIONS = (
    AUTHZ_COMPATIBILITY_FLOOR_REVISION,
    PRE_RETIREMENT_ALEMBIC_REVISION,
    retired_preview.revision,
    retired_delivery.revision,
    retirement_join.revision,
    discussion_topics.revision,
    topics_join.revision,
    profile_archive.revision,
    preview_settings.revision,
    idle_retirement.revision,
    idle_hostnames_join.revision,
    idle_compatibility_join.revision,
    integration_attestations.revision,
)
_RECOGNIZED_SOURCE_REVISIONS = (
    *SCHEMA_MIGRATION_SUPPORTED_REVISIONS,
    public_host_fences.revision,
    public_host_join.revision,
    topics_hostnames.revision,
    compatibility_join.revision,
    archive_retirement_join.revision,
    archive_hostnames_join.revision,
    archive_compatibility_join.revision,
    preview_retirement_join.revision,
    preview_hostnames_join.revision,
    preview_compatibility_join.revision,
    idle_retirement_join.revision,
    idle_hostnames_join.revision,
    idle_compatibility_join.revision,
    attestations_compatibility_join.revision,
)
_ARCHIVE_JOIN_BY_SOURCE = {
    retired_delivery.revision: archive_retirement_join.revision,
    retirement_join.revision: archive_retirement_join.revision,
    topics_join.revision: archive_retirement_join.revision,
    public_host_fences.revision: archive_hostnames_join.revision,
    topics_hostnames.revision: archive_hostnames_join.revision,
    public_host_join.revision: archive_compatibility_join.revision,
    compatibility_join.revision: archive_compatibility_join.revision,
}
_IDLE_JOIN_BY_SOURCE = {
    **{
        source: {
            archive_retirement_join.revision: idle_retirement_join.revision,
            archive_hostnames_join.revision: idle_hostnames_join.revision,
            archive_compatibility_join.revision: idle_compatibility_join.revision,
        }[target]
        for source, target in _ARCHIVE_JOIN_BY_SOURCE.items()
    },
    archive_retirement_join.revision: idle_retirement_join.revision,
    archive_hostnames_join.revision: idle_hostnames_join.revision,
    archive_compatibility_join.revision: idle_compatibility_join.revision,
    preview_retirement_join.revision: idle_retirement_join.revision,
    preview_hostnames_join.revision: idle_hostnames_join.revision,
    preview_compatibility_join.revision: idle_compatibility_join.revision,
}

_HELD_DELIVERY_TARGETS = frozenset(
    {retired_delivery.revision, retirement_join.revision, topics_join.revision}
)
_SCHEMA_MIGRATION_LOCK_KEY = "launchplane:schema-migration"
SchemaMigrationAction = Literal["current", "upgrade", "compatible_ahead"]


def schema_migration_action(
    *,
    current_revision: str,
    target_revision: str = SCHEMA_MIGRATION_TARGET_REVISION,
) -> SchemaMigrationAction:
    _require_supported_target(target_revision)
    effective_target = schema_migration_target(
        current_revision=current_revision, target_revision=target_revision
    )
    if current_revision == effective_target:
        return "current"
    if current_revision in _RECOGNIZED_SOURCE_REVISIONS:
        if effective_target in _alembic_predecessor_revisions(current_revision):
            return "compatible_ahead"
    if current_revision in _alembic_predecessor_revisions(effective_target):
        return "upgrade"
    raise ValueError(f"Unsupported Launchplane database revision {current_revision!r}.")


def schema_migration_target(*, current_revision: str, target_revision: str) -> str:
    if target_revision == integration_attestations.revision:
        if current_revision in _RECOGNIZED_SOURCE_REVISIONS and (
            current_revision == retired_delivery.revision
            or retired_delivery.revision in _alembic_predecessor_revisions(current_revision)
        ):
            return attestations_compatibility_join.revision
        return target_revision
    if target_revision == idle_hostnames_join.revision:
        if current_revision in _RECOGNIZED_SOURCE_REVISIONS and (
            current_revision == retired_delivery.revision
            or retired_delivery.revision in _alembic_predecessor_revisions(current_revision)
        ):
            return idle_compatibility_join.revision
        return target_revision
    if target_revision in _HELD_DELIVERY_TARGETS and current_revision in {
        public_host_fences.revision,
        topics_hostnames.revision,
        archive_hostnames_join.revision,
        preview_hostnames_join.revision,
        idle_hostnames_join.revision,
        integration_attestations.revision,
    }:
        return (
            attestations_compatibility_join.revision
            if current_revision == integration_attestations.revision
            else idle_compatibility_join.revision
        )
    if target_revision == idle_retirement.revision and current_revision in _IDLE_JOIN_BY_SOURCE:
        return _IDLE_JOIN_BY_SOURCE[current_revision]
    if current_revision == idle_retirement.revision and target_revision in _HELD_DELIVERY_TARGETS:
        return idle_retirement_join.revision
    if target_revision == preview_settings.revision:
        previous_join = _ARCHIVE_JOIN_BY_SOURCE.get(current_revision, current_revision)
        preview_join = {
            archive_retirement_join.revision: preview_retirement_join.revision,
            archive_hostnames_join.revision: preview_hostnames_join.revision,
            archive_compatibility_join.revision: preview_compatibility_join.revision,
        }.get(previous_join)
        if preview_join is not None:
            return preview_join
    if current_revision == preview_settings.revision and target_revision in _HELD_DELIVERY_TARGETS:
        return preview_retirement_join.revision
    if target_revision == profile_archive.revision and current_revision in _ARCHIVE_JOIN_BY_SOURCE:
        return _ARCHIVE_JOIN_BY_SOURCE[current_revision]
    if current_revision == profile_archive.revision and target_revision in _HELD_DELIVERY_TARGETS:
        return archive_retirement_join.revision
    # Only a database that has ALREADY applied the held delivery branch may
    # auto-join it. Explicitly requesting that branch remains the approved-host
    # application path; when preview has applied, join both on that request.
    if {current_revision, target_revision} == {retired_delivery.revision, retired_preview.revision}:
        return topics_join.revision
    if (
        current_revision in {retired_delivery.revision, retirement_join.revision}
        and target_revision == discussion_topics.revision
    ) or (
        current_revision == discussion_topics.revision
        and target_revision in {retired_delivery.revision, retirement_join.revision}
    ):
        return topics_join.revision
    if (
        current_revision == public_host_fences.revision
        and target_revision == discussion_topics.revision
    ):
        return topics_hostnames.revision
    if current_revision == public_host_join.revision and target_revision in {
        discussion_topics.revision,
        topics_join.revision,
    }:
        return compatibility_join.revision
    return target_revision


def _require_supported_target(target_revision: str) -> None:
    if target_revision not in SCHEMA_MIGRATION_SUPPORTED_REVISIONS:
        raise ValueError(f"Unsupported Launchplane schema migration target {target_revision!r}.")


def _alembic_predecessor_revisions(target_revision: str) -> frozenset[str]:
    script_directory = ScriptDirectory.from_config(alembic_config("sqlite://"))
    return frozenset(
        revision.revision
        for revision in script_directory.iterate_revisions(target_revision, "base")
    )


def migrate_schema_on_connection(connection: Connection, *, target_revision: str) -> str:
    _require_supported_target(target_revision)
    if connection.dialect.name != "postgresql" or not connection.in_transaction():
        raise RuntimeError("A held PostgreSQL transaction is required.")
    revisions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    if len(revisions) != 1:
        raise RuntimeError("The held database must have exactly one migration revision.")
    current = str(revisions[0])
    action = schema_migration_action(current_revision=current, target_revision=target_revision)
    if action == "upgrade":
        config = alembic_config("postgresql://")
        config.attributes["connection"] = connection
        command.upgrade(
            config,
            schema_migration_target(current_revision=current, target_revision=target_revision),
        )
    final = str(connection.scalar(text("SELECT version_num FROM alembic_version")))
    if schema_migration_action(current_revision=final, target_revision=target_revision) not in {
        "current",
        "compatible_ahead",
    }:
        raise RuntimeError("The held migration did not reach a compatible revision.")
    return final


def migrate_schema(
    *,
    database_url: str,
    target_revision: str = SCHEMA_MIGRATION_TARGET_REVISION,
) -> str:
    _require_supported_target(target_revision)
    engine = _build_engine(database_url)
    try:
        if engine.url.get_backend_name() != "postgresql":
            raise RuntimeError("Launchplane hosted schema migration requires PostgreSQL.")
        with engine.connect() as lock_connection:
            lock_connection = lock_connection.execution_options(isolation_level="AUTOCOMMIT")
            lock_connection.execute(
                text("select pg_advisory_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": _SCHEMA_MIGRATION_LOCK_KEY},
            )
            try:
                stamp_revision = schema_stamp_revision_for_engine(engine)
                if stamp_revision:
                    command.stamp(alembic_config(database_url), stamp_revision)
                current_revision = _current_revision(engine)
                if not current_revision:
                    command.upgrade(alembic_config(database_url), target_revision)
                else:
                    action = schema_migration_action(
                        current_revision=current_revision,
                        target_revision=target_revision,
                    )
                    if action == "upgrade":
                        command.upgrade(
                            alembic_config(database_url),
                            schema_migration_target(
                                current_revision=current_revision, target_revision=target_revision
                            ),
                        )
                final_revision = _current_revision(engine)
                if not final_revision:
                    raise RuntimeError("Launchplane schema migration did not record a revision.")
                final_action = schema_migration_action(
                    current_revision=final_revision,
                    target_revision=target_revision,
                )
                if final_action not in {"current", "compatible_ahead"}:
                    raise RuntimeError(
                        "Launchplane schema migration did not reach a compatible revision."
                    )
                store = PostgresRecordStore(database_url=database_url)
                try:
                    store.seed_guest_capture_bindings()
                finally:
                    store.close()
                return final_revision
            finally:
                lock_connection.execute(
                    text("select pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                    {"lock_key": _SCHEMA_MIGRATION_LOCK_KEY},
                )
    finally:
        engine.dispose()


def _current_revision(engine: Engine) -> str:
    if "alembic_version" not in set(inspect(engine).get_table_names()):
        return ""
    with engine.connect() as connection:
        rows = connection.execute(text("select version_num from alembic_version")).fetchall()
    revisions = tuple(str(row[0]).strip() for row in rows if str(row[0]).strip())
    if len(revisions) > 1:
        raise RuntimeError(
            "Launchplane database has multiple Alembic heads: " + ", ".join(revisions)
        )
    return revisions[0] if revisions else ""


def alembic_config(database_url: str) -> Config:
    repository_root = Path(__file__).resolve().parents[2]
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Migrate the configured Launchplane database.")
    parser.add_argument(
        "--target-revision",
        choices=SCHEMA_MIGRATION_SUPPORTED_REVISIONS,
        default=SCHEMA_MIGRATION_TARGET_REVISION,
        help="Explicit approved host-side migration target; startup uses the release default.",
    )
    args = parser.parse_args(argv)
    database_url = os.environ.get("LAUNCHPLANE_DATABASE_URL", "").strip()
    if not database_url:
        print("LAUNCHPLANE_DATABASE_URL is required for schema migration.", file=sys.stderr)
        return 1
    try:
        revision = migrate_schema(database_url=database_url, target_revision=args.target_revision)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    print(revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
