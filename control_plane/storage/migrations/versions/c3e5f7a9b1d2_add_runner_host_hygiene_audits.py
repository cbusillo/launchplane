"""add runner host hygiene audits

Revision ID: c3e5f7a9b1d2
Revises: c2d4e6f8a0b2
Create Date: 2026-05-23 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c3e5f7a9b1d2"
down_revision: str | None = "c2d4e6f8a0b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_runner_host_hygiene_audits"
_HOST_INDEX = "launchplane_runner_host_hygiene_audits_host_idx"
_STATUS_INDEX = "launchplane_runner_host_hygiene_audits_status_idx"


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
            sa.Column("audit_record_key", sa.String(), nullable=False),
            sa.Column("host_name", sa.String(), nullable=False),
            sa.Column("action", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("mutate", sa.Integer(), nullable=False),
            sa.Column(
                "payload",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("audit_record_key"),
        )
    if not _index_exists(_TABLE, _HOST_INDEX):
        op.create_index(
            _HOST_INDEX,
            _TABLE,
            ["host_name", sa.text("audit_record_key DESC")],
        )
    if not _index_exists(_TABLE, _STATUS_INDEX):
        op.create_index(
            _STATUS_INDEX,
            _TABLE,
            ["status", sa.text("audit_record_key DESC")],
        )


def downgrade() -> None:
    op.drop_index(_STATUS_INDEX, table_name=_TABLE)
    op.drop_index(_HOST_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
