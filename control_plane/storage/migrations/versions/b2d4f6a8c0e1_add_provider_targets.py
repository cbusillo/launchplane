"""add provider targets

Revision ID: b2d4f6a8c0e1
Revises: a9c1e3f5b7d9
Create Date: 2026-06-01 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b2d4f6a8c0e1"
down_revision: str | None = "a9c1e3f5b7d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_provider_targets"
_PROVIDER_INDEX = "launchplane_provider_targets_provider_idx"
_UPDATED_INDEX = "launchplane_provider_targets_updated_idx"


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
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("instance", sa.String(), nullable=False),
            sa.Column("provider_id", sa.String(), nullable=False),
            sa.Column("target_category", sa.String(), nullable=False),
            sa.Column("target_id", sa.String(), nullable=False),
            sa.Column("display_name", sa.String(), nullable=False),
            sa.Column("provider_target_type", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column(
                "payload",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("context", "instance"),
        )
    if not _index_exists(_TABLE, _PROVIDER_INDEX):
        op.create_index(
            _PROVIDER_INDEX,
            _TABLE,
            ["provider_id", sa.text("updated_at DESC")],
        )
    if not _index_exists(_TABLE, _UPDATED_INDEX):
        op.create_index(
            _UPDATED_INDEX,
            _TABLE,
            [sa.text("updated_at DESC")],
        )


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if _index_exists(_TABLE, _UPDATED_INDEX):
        op.drop_index(_UPDATED_INDEX, table_name=_TABLE)
    if _index_exists(_TABLE, _PROVIDER_INDEX):
        op.drop_index(_PROVIDER_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
