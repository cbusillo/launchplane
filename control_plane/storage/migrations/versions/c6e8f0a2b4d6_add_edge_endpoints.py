"""add edge endpoint records

Revision ID: c6e8f0a2b4d6
Revises: c5e7f9a1b3d5
Create Date: 2026-06-07 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c6e8f0a2b4d6"
down_revision: str | None = "c5e7f9a1b3d5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_edge_endpoints"
_PROVIDER_STATUS_INDEX = "launchplane_edge_endpoints_provider_status_idx"


def _table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return table_name in set(inspector.get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return index_name in {str(index["name"]) for index in inspector.get_indexes(table_name)}


def _payload_column() -> sa.Column[object]:
    return sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        nullable=False,
    )


def upgrade() -> None:
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("endpoint_key", sa.String(), nullable=False),
            sa.Column("provider", sa.String(), nullable=False),
            sa.Column("server_name", sa.String(), nullable=False),
            sa.Column("upstream_host", sa.String(), nullable=False),
            sa.Column("upstream_scheme", sa.String(), nullable=False),
            sa.Column("upstream_port", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            _payload_column(),
            sa.PrimaryKeyConstraint("endpoint_key"),
        )
    if not _index_exists(_TABLE, _PROVIDER_STATUS_INDEX):
        op.create_index(
            _PROVIDER_STATUS_INDEX,
            _TABLE,
            ["provider", "status", "endpoint_key"],
        )


def downgrade() -> None:
    if _table_exists(_TABLE):
        if _index_exists(_TABLE, _PROVIDER_STATUS_INDEX):
            op.drop_index(_PROVIDER_STATUS_INDEX, table_name=_TABLE)
        op.drop_table(_TABLE)
