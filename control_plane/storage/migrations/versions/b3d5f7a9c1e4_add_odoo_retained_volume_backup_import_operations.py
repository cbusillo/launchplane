"""add Odoo retained-volume backup import operations

Revision ID: b3d5f7a9c1e4
Revises: a1c3e5f7b9d2
Create Date: 2026-07-26 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b3d5f7a9c1e4"
down_revision: str | None = "a1c3e5f7b9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_odoo_prod_retained_volume_backup_import_operations"
_LANE_STATUS_INDEX = "launchplane_odoo_retained_import_operation_lane_status_idx"
_IDEMPOTENCY_INDEX = "launchplane_odoo_retained_import_operation_idempotency_idx"
_ACTIVE_LANE_INDEX = "launchplane_odoo_retained_import_active_lane_uidx"
_WORKER_CLAIM_INDEX = "launchplane_odoo_retained_import_worker_claim_idx"


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in set(inspector.get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return index_name in {str(index["name"]) for index in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("operation_id", sa.String(), nullable=False),
            sa.Column("operation_kind", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("instance", sa.String(), nullable=False),
            sa.Column("idempotency_key", sa.String(), nullable=False),
            sa.Column("idempotency_scope", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("phase", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("lease_owner", sa.String(), nullable=False, server_default=""),
            sa.Column("lease_expires_at", sa.String(), nullable=False, server_default=""),
            sa.Column("heartbeat_at", sa.String(), nullable=False, server_default=""),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "payload",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
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
    if not _index_exists(_TABLE, _IDEMPOTENCY_INDEX):
        op.create_index(
            _IDEMPOTENCY_INDEX,
            _TABLE,
            [
                "operation_kind",
                "idempotency_scope",
                "idempotency_key",
                sa.text("updated_at DESC"),
            ],
        )
    if not _index_exists(_TABLE, _ACTIVE_LANE_INDEX):
        op.create_index(
            _ACTIVE_LANE_INDEX,
            _TABLE,
            ["product", "context", "instance"],
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
    if _index_exists(_TABLE, _ACTIVE_LANE_INDEX):
        op.drop_index(_ACTIVE_LANE_INDEX, table_name=_TABLE)
    if _index_exists(_TABLE, _IDEMPOTENCY_INDEX):
        op.drop_index(_IDEMPOTENCY_INDEX, table_name=_TABLE)
    if _index_exists(_TABLE, _LANE_STATUS_INDEX):
        op.drop_index(_LANE_STATUS_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
