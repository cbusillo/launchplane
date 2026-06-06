"""add Odoo bootstrap active lane reservation index

Revision ID: c5e7f9a1b3d5
Revises: b2d4f6a8c0e1
Create Date: 2026-06-06 00:35:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "c5e7f9a1b3d5"
down_revision: str | None = "b2d4f6a8c0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_odoo_stable_bootstrap_operations"
_ACTIVE_LANE_INDEX = "launchplane_odoo_bootstrap_active_lane_uidx"


def _index_exists(table_name: str, index_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return index_name in {str(index["name"]) for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
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
    if _index_exists(_TABLE, _ACTIVE_LANE_INDEX):
        op.drop_index(_ACTIVE_LANE_INDEX, table_name=_TABLE)
