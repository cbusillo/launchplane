"""fence active provider mutations

Revision ID: b4d6f8a0c2e4
Revises: a2b4c6d8e0f2
Create Date: 2026-07-13 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b4d6f8a0c2e4"
down_revision: str | None = "a2b4c6d8e0f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_idempotency_records"
_INDEX = "launchplane_idempotency_active_reconciliation_idx"
_TARGET_KEY_COLUMN = "provider_target_key"
_ACTIVE_PREDICATE = "provider_target_key <> '' AND state IN ('running', 'reconcile_required')"


def _index_exists() -> bool:
    inspector = sa.inspect(op.get_bind())
    return _INDEX in {str(index["name"]) for index in inspector.get_indexes(_TABLE)}


def _column_exists() -> bool:
    inspector = sa.inspect(op.get_bind())
    return _TARGET_KEY_COLUMN in {str(column["name"]) for column in inspector.get_columns(_TABLE)}


def _assert_no_active_target_collisions() -> None:
    collision = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT provider_target_key, COUNT(*) AS claim_count "
                "FROM launchplane_idempotency_records "
                "WHERE provider_target_key <> '' "
                "AND state IN ('running', 'reconcile_required') "
                "GROUP BY provider_target_key "
                "HAVING COUNT(*) > 1 "
                "ORDER BY provider_target_key "
                "LIMIT 1"
            )
        )
        .mappings()
        .first()
    )
    if collision is not None:
        raise RuntimeError(
            "Cannot fence active provider mutations while duplicate target claims exist: "
            f"{collision['provider_target_key']!r} has {collision['claim_count']} claims. "
            "Reconcile the duplicate claims before upgrading."
        )


def upgrade() -> None:
    if not _column_exists():
        op.add_column(
            _TABLE,
            sa.Column(
                _TARGET_KEY_COLUMN,
                sa.String(),
                nullable=False,
                server_default="",
            ),
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            sa.text(
                "UPDATE launchplane_idempotency_records "
                "SET provider_target_key = reconciliation_key, "
                "payload = jsonb_set(payload, '{provider_target_key}', "
                "to_jsonb(reconciliation_key), true) "
                "WHERE reconciliation_key <> ''"
            )
        )
    else:
        op.execute(
            sa.text(
                "UPDATE launchplane_idempotency_records "
                "SET provider_target_key = reconciliation_key, "
                "payload = json_set(payload, '$.provider_target_key', reconciliation_key) "
                "WHERE reconciliation_key <> ''"
            )
        )
    _assert_no_active_target_collisions()
    if not _index_exists():
        op.create_index(
            _INDEX,
            _TABLE,
            [_TARGET_KEY_COLUMN],
            unique=True,
            postgresql_where=sa.text(_ACTIVE_PREDICATE),
            sqlite_where=sa.text(_ACTIVE_PREDICATE),
        )


def downgrade() -> None:
    if _index_exists():
        op.drop_index(_INDEX, table_name=_TABLE)
    if _column_exists():
        op.drop_column(_TABLE, _TARGET_KEY_COLUMN)
