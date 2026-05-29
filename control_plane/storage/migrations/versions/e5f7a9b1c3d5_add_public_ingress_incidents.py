"""add public ingress incidents

Revision ID: e5f7a9b1c3d5
Revises: d4e6f8a0b2c4
Create Date: 2026-05-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e5f7a9b1c3d5"
down_revision: str | None = "d4e6f8a0b2c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_public_ingress_incidents"
_LOOKUP_INDEX = "launchplane_public_ingress_incidents_lookup_idx"
_STATUS_INDEX = "launchplane_public_ingress_incidents_status_idx"


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
            sa.Column("incident_id", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("instance", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("opened_at", sa.String(), nullable=False),
            sa.Column("latest_observed_at", sa.String(), nullable=False),
            sa.Column(
                "payload",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("incident_id"),
        )
    if not _index_exists(_TABLE, _LOOKUP_INDEX):
        op.create_index(
            _LOOKUP_INDEX,
            _TABLE,
            ["product", "context", "instance", "status", sa.text("opened_at DESC")],
        )
    if not _index_exists(_TABLE, _STATUS_INDEX):
        op.create_index(
            _STATUS_INDEX,
            _TABLE,
            ["status", sa.text("opened_at DESC")],
        )


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if _index_exists(_TABLE, _STATUS_INDEX):
        op.drop_index(_STATUS_INDEX, table_name=_TABLE)
    if _index_exists(_TABLE, _LOOKUP_INDEX):
        op.drop_index(_LOOKUP_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
