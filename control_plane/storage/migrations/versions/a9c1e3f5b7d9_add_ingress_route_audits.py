"""add ingress route audits

Revision ID: a9c1e3f5b7d9
Revises: f8a0b2c3d4e5
Create Date: 2026-05-31 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a9c1e3f5b7d9"
down_revision: str | None = "f8a0b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_ingress_route_audits"
_LOOKUP_INDEX = "launchplane_ingress_route_audits_lookup_idx"
_STATUS_INDEX = "launchplane_ingress_route_audits_status_idx"


def _table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return table_name in set(inspector.get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return index_name in {str(index["name"]) for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("mode", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("provider_host_id", sa.Integer(), nullable=True),
            sa.Column("recorded_at", sa.String(), nullable=False),
            sa.Column(
                "payload",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
    if not _index_exists(_TABLE, _LOOKUP_INDEX):
        op.create_index(
            _LOOKUP_INDEX,
            _TABLE,
            ["product", "context", sa.text("recorded_at DESC")],
        )
    if not _index_exists(_TABLE, _STATUS_INDEX):
        op.create_index(
            _STATUS_INDEX,
            _TABLE,
            ["status", sa.text("recorded_at DESC")],
        )


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if _index_exists(_TABLE, _STATUS_INDEX):
        op.drop_index(_STATUS_INDEX, table_name=_TABLE)
    if _index_exists(_TABLE, _LOOKUP_INDEX):
        op.drop_index(_LOOKUP_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
