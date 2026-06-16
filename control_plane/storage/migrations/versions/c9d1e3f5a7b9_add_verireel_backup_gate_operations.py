"""add verireel backup gate operations

Revision ID: c9d1e3f5a7b9
Revises: b7d9f1a3c5e7
Create Date: 2026-06-16 13:30:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c9d1e3f5a7b9"
down_revision: str | None = "b7d9f1a3c5e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_verireel_prod_backup_gate_operations"
_LANE_STATUS_INDEX = "launchplane_verireel_backup_gate_operation_lane_status_idx"
_RECORD_INDEX = "launchplane_verireel_backup_gate_operation_record_idx"
_ACTIVE_RECORD_INDEX = "launchplane_verireel_backup_gate_active_record_uidx"
_WORKER_CLAIM_INDEX = "launchplane_verireel_backup_gate_worker_claim_idx"


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
            sa.Column("operation_id", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("instance", sa.String(), nullable=False),
            sa.Column("backup_record_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("phase", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("lease_owner", sa.String(), server_default="", nullable=False),
            sa.Column("lease_expires_at", sa.String(), server_default="", nullable=False),
            sa.Column("heartbeat_at", sa.String(), server_default="", nullable=False),
            sa.Column("attempt", sa.Integer(), server_default="0", nullable=False),
            sa.Column(
                "payload",
                sa.JSON().with_variant(
                    postgresql.JSONB(astext_type=sa.Text()), "postgresql"
                ),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("operation_id"),
        )
    if not _index_exists(_TABLE, _LANE_STATUS_INDEX):
        op.create_index(
            _LANE_STATUS_INDEX,
            _TABLE,
            ["product", "context", "instance", "status", sa.text("updated_at DESC")],
        )
    if not _index_exists(_TABLE, _RECORD_INDEX):
        op.create_index(
            _RECORD_INDEX,
            _TABLE,
            ["backup_record_id", sa.text("updated_at DESC")],
        )
    if not _index_exists(_TABLE, _ACTIVE_RECORD_INDEX):
        op.create_index(
            _ACTIVE_RECORD_INDEX,
            _TABLE,
            ["backup_record_id"],
            unique=True,
            postgresql_where=sa.text("status IN ('pending', 'running')"),
            sqlite_where=sa.text("status IN ('pending', 'running')"),
        )
    if not _index_exists(_TABLE, _WORKER_CLAIM_INDEX):
        op.create_index(
            _WORKER_CLAIM_INDEX,
            _TABLE,
            ["status", "lease_expires_at", "updated_at"],
        )


def downgrade() -> None:
    if not _table_exists(_TABLE):
        return
    if _index_exists(_TABLE, _WORKER_CLAIM_INDEX):
        op.drop_index(_WORKER_CLAIM_INDEX, table_name=_TABLE)
    if _index_exists(_TABLE, _ACTIVE_RECORD_INDEX):
        op.drop_index(_ACTIVE_RECORD_INDEX, table_name=_TABLE)
    if _index_exists(_TABLE, _RECORD_INDEX):
        op.drop_index(_RECORD_INDEX, table_name=_TABLE)
    if _index_exists(_TABLE, _LANE_STATUS_INDEX):
        op.drop_index(_LANE_STATUS_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
