"""fence active authz policy revisions

Revision ID: f4c6e8a0b2d4
Revises: f3b5d7e9a1c2
Create Date: 2026-07-18 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from alembic import op
import sqlalchemy as sa

revision: str = "f4c6e8a0b2d4"
down_revision: str | None = "f3b5d7e9a1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_authz_policies"
_REVISION_INDEX = "launchplane_authz_policies_revision_uidx"
_ACTIVE_INDEX = "launchplane_authz_policies_active_uidx"
_WRITE_TRIGGER = "launchplane_authz_policy_write_fence"
_WRITE_FUNCTION = "launchplane_fence_authz_policy_write"
_SQLITE_ACTIVE_INSERT_TRIGGER = "launchplane_authz_policy_active_insert_fence"
_SQLITE_ACTIVE_UPDATE_TRIGGER = "launchplane_authz_policy_active_update_fence"
_SQLITE_REVISION_TRIGGER = "launchplane_authz_policy_revision_allocator"


def _index_exists(index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return index_name in {str(index["name"]) for index in inspector.get_indexes(_TABLE)}


def _revision_column() -> dict[str, Any] | None:
    inspector = sa.inspect(op.get_bind())
    return next(
        (dict(column) for column in inspector.get_columns(_TABLE) if column["name"] == "revision"),
        None,
    )


def _canonicalize_records(*, include_revision: bool) -> None:
    connection = op.get_bind()
    policy_table = sa.table(
        _TABLE,
        sa.column("record_id", sa.String()),
        sa.column("revision", sa.BigInteger()),
        sa.column("status", sa.String()),
        sa.column("updated_at", sa.String()),
        sa.column("payload", sa.JSON()),
    )
    rows = tuple(
        connection.execute(
            sa.select(
                policy_table.c.record_id,
                policy_table.c.status,
                policy_table.c.updated_at,
                policy_table.c.payload,
            )
        ).mappings()
    )
    rows_with_timestamps = tuple(
        (row, _legacy_updated_at(str(row["updated_at"]), record_id=str(row["record_id"])))
        for row in rows
    )
    ordered_rows = tuple(
        row
        for row, _ in sorted(
            rows_with_timestamps,
            key=lambda item: (item[1], str(item[0]["record_id"])),
        )
    )
    active_timestamps = tuple(
        timestamp for row, timestamp in rows_with_timestamps if row["status"] == "active"
    )
    if len(active_timestamps) > 1 and active_timestamps.count(max(active_timestamps)) > 1:
        raise RuntimeError(
            "Cannot canonicalize Launchplane authz policy history because multiple active "
            "records share the latest updated_at timestamp. Resolve the active record "
            "explicitly before applying this migration."
        )
    active_record_ids = tuple(
        str(row["record_id"]) for row in ordered_rows if row["status"] == "active"
    )
    retained_active_record_id = active_record_ids[-1] if active_record_ids else ""
    for record_revision, row in enumerate(ordered_rows, start=1):
        record_id = str(row["record_id"])
        status = str(row["status"])
        if status == "active" and record_id != retained_active_record_id:
            status = "superseded"
        payload: dict[str, Any] = dict(row["payload"] or {})
        payload["status"] = status
        payload.pop("revision", None)
        values: dict[str, object] = {"status": status, "payload": payload}
        if include_revision:
            values["revision"] = record_revision
        connection.execute(
            policy_table.update().where(policy_table.c.record_id == record_id).values(**values)
        )


def _legacy_updated_at(value: str, *, record_id: str) -> datetime:
    normalized_value = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError as error:
        raise RuntimeError(
            f"Cannot canonicalize Launchplane authz policy {record_id!r}: invalid updated_at."
        ) from error
    if parsed.tzinfo is None:
        raise RuntimeError(
            f"Cannot canonicalize Launchplane authz policy {record_id!r}: updated_at must "
            "include a timezone."
        )
    return parsed.astimezone(timezone.utc)


def _create_sqlite_write_fence() -> None:
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER IF NOT EXISTS {_SQLITE_ACTIVE_INSERT_TRIGGER}
            BEFORE INSERT ON {_TABLE}
            WHEN NEW.status = 'active'
            BEGIN
                UPDATE {_TABLE}
                SET status = 'superseded',
                    payload = json_set(payload, '$.status', 'superseded')
                WHERE status = 'active'
                  AND record_id <> NEW.record_id;
            END
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER IF NOT EXISTS {_SQLITE_ACTIVE_UPDATE_TRIGGER}
            BEFORE UPDATE OF status ON {_TABLE}
            WHEN NEW.status = 'active'
            BEGIN
                UPDATE {_TABLE}
                SET status = 'superseded',
                    payload = json_set(payload, '$.status', 'superseded')
                WHERE status = 'active'
                  AND record_id <> NEW.record_id;
            END
            """
        )
    )
    op.execute(
        sa.text(
            f"""
            CREATE TRIGGER IF NOT EXISTS {_SQLITE_REVISION_TRIGGER}
            AFTER INSERT ON {_TABLE}
            WHEN NEW.revision = 0
            BEGIN
                UPDATE {_TABLE}
                SET revision = (
                    SELECT COALESCE(MAX(revision), 0) + 1
                    FROM {_TABLE}
                    WHERE record_id <> NEW.record_id
                )
                WHERE record_id = NEW.record_id;
            END
            """
        )
    )


def upgrade() -> None:
    revision_column = _revision_column()
    if revision_column is None:
        op.add_column(_TABLE, sa.Column("revision", sa.BigInteger(), nullable=True))
    _canonicalize_records(include_revision=True)
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            sa.text(
                f"""
                CREATE OR REPLACE FUNCTION {_WRITE_FUNCTION}()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $$
                BEGIN
                    PERFORM pg_advisory_xact_lock(
                        hashtextextended('launchplane:active-authz-policy', 0)
                    );
                    IF NEW.revision IS NULL THEN
                        SELECT COALESCE(MAX(revision), 0) + 1
                        INTO NEW.revision
                        FROM {_TABLE};
                    END IF;
                    IF NEW.status = 'active' THEN
                        UPDATE {_TABLE}
                        SET status = 'superseded',
                            payload = jsonb_set(
                                payload,
                                '{{status}}',
                                '"superseded"'::jsonb,
                                true
                            )
                        WHERE status = 'active'
                          AND record_id <> NEW.record_id;
                    END IF;
                    RETURN NEW;
                END;
                $$
                """
            )
        )
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_WRITE_TRIGGER} ON {_TABLE}"))
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {_WRITE_TRIGGER}
                BEFORE INSERT OR UPDATE OF status ON {_TABLE}
                FOR EACH ROW
                EXECUTE FUNCTION {_WRITE_FUNCTION}()
                """
            )
        )
    revision_column = _revision_column()
    if revision_column is not None and bool(revision_column.get("nullable")):
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table(_TABLE) as batch_op:
                batch_op.alter_column(
                    "revision",
                    existing_type=sa.BigInteger(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
        else:
            op.alter_column(_TABLE, "revision", existing_type=sa.BigInteger(), nullable=False)
    if op.get_bind().dialect.name == "sqlite":
        _create_sqlite_write_fence()
    if not _index_exists(_REVISION_INDEX):
        op.create_index(_REVISION_INDEX, _TABLE, ["revision"], unique=True)
    if not _index_exists(_ACTIVE_INDEX):
        op.create_index(
            _ACTIVE_INDEX,
            _TABLE,
            ["status"],
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_WRITE_TRIGGER} ON {_TABLE}"))
        op.execute(sa.text(f"DROP FUNCTION IF EXISTS {_WRITE_FUNCTION}()"))
    else:
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_SQLITE_REVISION_TRIGGER}"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_SQLITE_ACTIVE_UPDATE_TRIGGER}"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_SQLITE_ACTIVE_INSERT_TRIGGER}"))
    if _index_exists(_ACTIVE_INDEX):
        op.drop_index(_ACTIVE_INDEX, table_name=_TABLE)
    if _index_exists(_REVISION_INDEX):
        op.drop_index(_REVISION_INDEX, table_name=_TABLE)
    if _revision_column() is not None:
        _canonicalize_records(include_revision=False)
        if op.get_bind().dialect.name == "sqlite":
            with op.batch_alter_table(_TABLE) as batch_op:
                batch_op.drop_column("revision")
        else:
            op.drop_column(_TABLE, "revision")
