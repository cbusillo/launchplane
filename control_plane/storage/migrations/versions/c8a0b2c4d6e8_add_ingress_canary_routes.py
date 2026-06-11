"""add ingress canary route records

Revision ID: c8a0b2c4d6e8
Revises: c7f9a1b3d5e7
Create Date: 2026-06-11 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c8a0b2c4d6e8"
down_revision: str | None = "c7f9a1b3d5e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_ingress_canary_routes"
_LOOKUP_INDEX = "launchplane_ingress_canary_routes_lookup_idx"


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
            sa.Column("canary_key", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("domain_name", sa.String(), nullable=False),
            sa.Column("expected_host_id", sa.Integer(), nullable=False),
            sa.Column("edge_endpoint_key", sa.String(), nullable=False),
            sa.Column("certificate_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            _payload_column(),
            sa.PrimaryKeyConstraint("canary_key"),
        )
    if not _index_exists(_TABLE, _LOOKUP_INDEX):
        op.create_index(
            _LOOKUP_INDEX,
            _TABLE,
            ["product", "context", "status", "canary_key"],
        )


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if _index_exists(_TABLE, _LOOKUP_INDEX):
        op.drop_index(_LOOKUP_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
