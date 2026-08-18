from __future__ import annotations

import os
from pathlib import Path
import sys
from typing import Literal

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from control_plane.storage.postgres import _build_engine
from control_plane.storage.schema_adoption import schema_stamp_revision_for_engine
from control_plane.storage.schema_invariants import (
    AUTHZ_COMPATIBILITY_FLOOR_REVISION,
    EXPECTED_ALEMBIC_HEAD_REVISION,
)

SCHEMA_MIGRATION_TARGET_REVISION = EXPECTED_ALEMBIC_HEAD_REVISION
SCHEMA_MIGRATION_SUPPORTED_REVISIONS = (
    AUTHZ_COMPATIBILITY_FLOOR_REVISION,
    EXPECTED_ALEMBIC_HEAD_REVISION,
)
_SCHEMA_MIGRATION_LOCK_KEY = "launchplane:schema-migration"
SchemaMigrationAction = Literal["current", "upgrade", "compatible_ahead"]


def schema_migration_action(
    *,
    current_revision: str,
    target_revision: str = SCHEMA_MIGRATION_TARGET_REVISION,
) -> SchemaMigrationAction:
    if target_revision not in SCHEMA_MIGRATION_SUPPORTED_REVISIONS:
        raise ValueError(f"Unsupported Launchplane schema migration target {target_revision!r}.")
    if current_revision == target_revision:
        return "current"
    if current_revision in SCHEMA_MIGRATION_SUPPORTED_REVISIONS:
        current_index = SCHEMA_MIGRATION_SUPPORTED_REVISIONS.index(current_revision)
        target_index = SCHEMA_MIGRATION_SUPPORTED_REVISIONS.index(target_revision)
        if current_index > target_index:
            return "compatible_ahead"
        return "upgrade"
    if current_revision in _alembic_predecessor_revisions(target_revision):
        return "upgrade"
    raise ValueError(f"Unsupported Launchplane database revision {current_revision!r}.")


def _alembic_predecessor_revisions(target_revision: str) -> frozenset[str]:
    script_directory = ScriptDirectory.from_config(alembic_config("sqlite://"))
    return frozenset(
        revision.revision
        for revision in script_directory.iterate_revisions(target_revision, "base")
    )


def migrate_schema(
    *,
    database_url: str,
    target_revision: str = SCHEMA_MIGRATION_TARGET_REVISION,
) -> str:
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
                        command.upgrade(alembic_config(database_url), target_revision)
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


def main() -> int:
    database_url = os.environ.get("LAUNCHPLANE_DATABASE_URL", "").strip()
    if not database_url:
        print("LAUNCHPLANE_DATABASE_URL is required for schema migration.", file=sys.stderr)
        return 1
    try:
        revision = migrate_schema(database_url=database_url)
    except Exception as error:
        print(str(error), file=sys.stderr)
        return 1
    print(revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
