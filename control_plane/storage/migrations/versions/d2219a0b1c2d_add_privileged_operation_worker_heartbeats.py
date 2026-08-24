"""add privileged-operation worker heartbeats

Revision ID: d2219a0b1c2d
Revises: c2221a0b1c2d
Create Date: 2026-08-24 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d2219a0b1c2d"
down_revision: str | None = "c2221a0b1c2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_privileged_operation_worker_heartbeats"
_FRESHNESS_INDEX = "launchplane_privop_worker_heartbeats_freshness_idx"


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
            sa.Column("worker_identity_sha256", sa.String(), nullable=False),
            sa.Column("worker_kind", sa.String(), nullable=False),
            sa.Column("image_reference", sa.String(), nullable=False),
            sa.Column("last_poll_succeeded_at", sa.String(), nullable=False),
            _payload_column(),
            sa.PrimaryKeyConstraint("worker_identity_sha256"),
        )
    if not _index_exists(_TABLE, _FRESHNESS_INDEX):
        op.create_index(
            _FRESHNESS_INDEX,
            _TABLE,
            ["worker_kind", "last_poll_succeeded_at"],
        )


def downgrade() -> None:
    if _table_exists(_TABLE):
        if _index_exists(_TABLE, _FRESHNESS_INDEX):
            op.drop_index(_FRESHNESS_INDEX, table_name=_TABLE)
        op.drop_table(_TABLE)
