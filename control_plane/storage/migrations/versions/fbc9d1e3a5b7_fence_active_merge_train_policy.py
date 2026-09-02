"""Fence active merge-train policy records.

Revision ID: fbc9d1e3a5b7
Revises: fb7d9e1a3c5f
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from alembic import op
import sqlalchemy as sa


revision: str = "fbc9d1e3a5b7"
down_revision: str | None = "fb7d9e1a3c5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_merge_train_policies"
_ACTIVE_INDEX = "launchplane_merge_train_policies_active_uidx"
_WRITE_TRIGGER = "launchplane_merge_train_policy_write_fence"
_WRITE_FUNCTION = "launchplane_fence_merge_train_policy_write"
_SQLITE_ACTIVE_INSERT_TRIGGER = "launchplane_merge_train_policy_active_insert_fence"
_SQLITE_ACTIVE_UPDATE_TRIGGER = "launchplane_merge_train_policy_active_update_fence"


def _index_exists(index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return index_name in {str(index["name"]) for index in inspector.get_indexes(_TABLE)}


def _legacy_updated_at(value: str, *, record_id: str) -> datetime:
    normalized_value = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError as error:
        raise RuntimeError(
            f"Cannot canonicalize Launchplane merge-train policy {record_id!r}: invalid updated_at."
        ) from error
    if parsed.tzinfo is None:
        raise RuntimeError(
            f"Cannot canonicalize Launchplane merge-train policy {record_id!r}: updated_at must "
            "include a timezone."
        )
    return parsed.astimezone(timezone.utc)


def _canonicalize_active_records() -> None:
    connection = op.get_bind()
    policy_table = sa.table(
        _TABLE,
        sa.column("record_id", sa.String()),
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
            "Cannot canonicalize Launchplane merge-train policy history because multiple "
            "active records share the latest updated_at timestamp. Resolve the active "
            "record explicitly before applying this migration."
        )
    active_record_ids = tuple(
        str(row["record_id"]) for row in ordered_rows if row["status"] == "active"
    )
    retained_active_record_id = active_record_ids[-1] if active_record_ids else ""
    for row in ordered_rows:
        record_id = str(row["record_id"])
        status = str(row["status"])
        if status == "active" and record_id != retained_active_record_id:
            status = "superseded"
            payload: dict[str, Any] = dict(row["payload"] or {})
            payload["status"] = status
            connection.execute(
                policy_table.update()
                .where(policy_table.c.record_id == record_id)
                .values(status=status, payload=payload)
            )


def upgrade() -> None:
    _canonicalize_active_records()
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
                        hashtextextended('launchplane:active-merge-train-policy', 0)
                    );
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
    else:
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
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_SQLITE_ACTIVE_UPDATE_TRIGGER}"))
        op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_SQLITE_ACTIVE_INSERT_TRIGGER}"))
    if _index_exists(_ACTIVE_INDEX):
        op.drop_index(_ACTIVE_INDEX, table_name=_TABLE)
