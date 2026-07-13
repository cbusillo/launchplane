"""add environment route binding records

Revision ID: e1f3a5c7b9d1
Revises: d0e2f4a6b8c0
Create Date: 2026-07-12 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e1f3a5c7b9d1"
down_revision: str | None = "d0e2f4a6b8c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_route_bindings"
_LOOKUP_INDEX = "launchplane_route_bindings_lookup_idx"
_UPDATED_INDEX = "launchplane_route_bindings_updated_idx"


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
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("instance", sa.String(), nullable=False),
            sa.Column("provider_id", sa.String(), nullable=False),
            sa.Column("target_category", sa.String(), nullable=False),
            sa.Column("ingress_provider", sa.String(), nullable=False),
            sa.Column("ingress_endpoint_key", sa.String(), nullable=False),
            sa.Column("termination_kind", sa.String(), nullable=False),
            sa.Column("tls_owner", sa.String(), nullable=False),
            sa.Column("primary_domain", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("freshness_status", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            _payload_column(),
            sa.PrimaryKeyConstraint("product", "context", "instance"),
        )
    if not _index_exists(_TABLE, _LOOKUP_INDEX):
        op.create_index(
            _LOOKUP_INDEX,
            _TABLE,
            ["product", "context", "status", "instance"],
        )
    if not _index_exists(_TABLE, _UPDATED_INDEX):
        op.create_index(_UPDATED_INDEX, _TABLE, [sa.text("updated_at DESC")])


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if _index_exists(_TABLE, _UPDATED_INDEX):
        op.drop_index(_UPDATED_INDEX, table_name=_TABLE)
    if _index_exists(_TABLE, _LOOKUP_INDEX):
        op.drop_index(_LOOKUP_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
