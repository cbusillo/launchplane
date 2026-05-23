"""repair Odoo target replacement operation scope column

Revision ID: c2d4e6f8a0b2
Revises: b1c3d5e7f9a1
Create Date: 2026-05-23 01:15:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c2d4e6f8a0b2"
down_revision: str | None = "b1c3d5e7f9a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_odoo_stable_target_replacement_operations"
_ACTIVE_LANE_INDEX = "launchplane_odoo_replacement_active_lane_uidx"
_IDEMPOTENCY_INDEX = "launchplane_odoo_replacement_operation_idempotency_idx"


def _column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return column_name in {str(column["name"]) for column in inspector.get_columns(table_name)}


def _index_exists(table_name: str, index_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return index_name in {str(index["name"]) for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _column_exists(_TABLE, "idempotency_scope"):
        op.add_column(
            _TABLE,
            sa.Column(
                "idempotency_scope",
                sa.String(),
                nullable=False,
                server_default="",
            ),
        )

    if _index_exists(_TABLE, _IDEMPOTENCY_INDEX):
        op.drop_index(_IDEMPOTENCY_INDEX, table_name=_TABLE)
    op.create_index(
        _IDEMPOTENCY_INDEX,
        _TABLE,
        ["idempotency_scope", "idempotency_key", sa.text("updated_at DESC")],
    )

    if not _index_exists(_TABLE, _ACTIVE_LANE_INDEX):
        op.create_index(
            _ACTIVE_LANE_INDEX,
            _TABLE,
            ["product", "context", "instance"],
            unique=True,
            postgresql_where=sa.text("status IN ('pending', 'running')"),
            sqlite_where=sa.text("status IN ('pending', 'running')"),
        )


def downgrade() -> None:
    return None
