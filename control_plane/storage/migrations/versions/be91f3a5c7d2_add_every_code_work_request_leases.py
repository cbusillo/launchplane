"""add Every Code work request leases

Revision ID: be91f3a5c7d2
Revises: d0e2f4a6b8c0
Create Date: 2026-07-13 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "be91f3a5c7d2"
down_revision: str | None = "d0e2f4a6b8c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_every_code_work_requests"
_LEASE_IDX = "launchplane_every_code_work_requests_lease_idx"


def _column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def _index_exists(table_name: str, index_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return index_name in {str(i["name"]) for i in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _column_exists(_TABLE, "lease_expires_at"):
        op.add_column(
            _TABLE,
            sa.Column("lease_expires_at", sa.String(), nullable=False, server_default=""),
        )
    if not _column_exists(_TABLE, "fencing_token"):
        op.add_column(
            _TABLE,
            sa.Column("fencing_token", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _column_exists(_TABLE, "attempt"):
        op.add_column(
            _TABLE,
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        )
    op.execute(
        sa.text(
            f"UPDATE {_TABLE} SET fencing_token = 1, attempt = 1 "
            "WHERE state IN ('claimed', 'running') AND fencing_token = 0"
        )
    )
    if not _index_exists(_TABLE, _LEASE_IDX):
        op.create_index(_LEASE_IDX, _TABLE, ["state", "lease_expires_at"])


def downgrade() -> None:
    if _index_exists(_TABLE, _LEASE_IDX):
        op.drop_index(_LEASE_IDX, table_name=_TABLE)
    if _column_exists(_TABLE, "attempt"):
        op.drop_column(_TABLE, "attempt")
    if _column_exists(_TABLE, "fencing_token"):
        op.drop_column(_TABLE, "fencing_token")
    if _column_exists(_TABLE, "lease_expires_at"):
        op.drop_column(_TABLE, "lease_expires_at")
