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
_ALL_ACTIVE_LANE_INDEXES = (
    (
        "launchplane_odoo_stable_bootstrap_operations",
        "launchplane_odoo_bootstrap_active_lane_uidx",
    ),
    (
        "launchplane_odoo_stable_target_replacement_operations",
        "launchplane_odoo_replacement_active_lane_uidx",
    ),
    (
        "launchplane_odoo_prod_backup_restore_operations",
        "launchplane_odoo_restore_active_lane_uidx",
    ),
    (_TABLE, _ACTIVE_LANE_INDEX),
)
_ACTIVE_LANE_PREDICATE = "status IN ('pending', 'running', 'reconciliation_required')"
_LEGACY_ACTIVE_LANE_PREDICATE = "status IN ('pending', 'running')"


def _table_exists(table_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table_name in set(inspector.get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return index_name in {str(index["name"]) for index in inspector.get_indexes(table_name)}


def _replace_active_lane_index(table_name: str, index_name: str, predicate: str) -> None:
    if not _table_exists(table_name):
        return
    if _index_exists(table_name, index_name):
        op.drop_index(index_name, table_name=table_name)
    op.create_index(
        index_name,
        table_name,
        ["product", "context", "instance"],
        unique=True,
        postgresql_where=sa.text(predicate),
        sqlite_where=sa.text(predicate),
    )


def _assert_no_cross_kind_blocking_lanes() -> None:
    table_names = tuple(
        table_name for table_name, _ in _ALL_ACTIVE_LANE_INDEXES if _table_exists(table_name)
    )
    if len(table_names) < 2:
        return
    union_sql = " UNION ALL ".join(
        f"SELECT product, context, instance FROM {table_name} "
        "WHERE status IN ('pending', 'running', 'reconciliation_required')"
        for table_name in table_names
    )
    duplicate_lane = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT product, context, instance FROM ("
                f"{union_sql}"
                ") AS active_odoo_stable_lane_operations "
                "GROUP BY product, context, instance HAVING COUNT(*) > 1 LIMIT 1"
            )
        )
        .first()
    )
    if duplicate_lane is not None:
        raise RuntimeError(
            "Odoo stable-lane operation migration found multiple blocking operation kinds for "
            "one product/context/instance; reconcile the existing queue before deployment."
        )


def _lock_blocking_operation_tables() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    table_names = tuple(
        table_name for table_name, _ in _ALL_ACTIVE_LANE_INDEXES if _table_exists(table_name)
    )
    if table_names:
        bind.execute(
            sa.text("LOCK TABLE " + ", ".join(table_names) + " IN SHARE ROW EXCLUSIVE MODE")
        )


def _assert_no_reconciliation_required_operations() -> None:
    for table_name, _ in _ALL_ACTIVE_LANE_INDEXES:
        if not _table_exists(table_name):
            continue
        operation_exists = (
            op.get_bind()
            .execute(
                sa.text(
                    f"SELECT 1 FROM {table_name} WHERE status = 'reconciliation_required' LIMIT 1"
                )
            )
            .first()
        )
        if operation_exists is not None:
            raise RuntimeError(
                "Cannot downgrade Odoo stable-lane operation fencing while reconciliation-required "
                "provider evidence exists."
            )


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
    _lock_blocking_operation_tables()
    _assert_no_cross_kind_blocking_lanes()
    for table_name, index_name in _ALL_ACTIVE_LANE_INDEXES:
        _replace_active_lane_index(table_name, index_name, _ACTIVE_LANE_PREDICATE)
    if not _index_exists(_TABLE, _WORKER_CLAIM_INDEX):
        op.create_index(
            _WORKER_CLAIM_INDEX,
            _TABLE,
            ["status", "lease_expires_at", "updated_at"],
        )


def downgrade() -> None:
    _lock_blocking_operation_tables()
    _assert_no_reconciliation_required_operations()
    for table_name, index_name in _ALL_ACTIVE_LANE_INDEXES[:-1]:
        _replace_active_lane_index(table_name, index_name, _LEGACY_ACTIVE_LANE_PREDICATE)
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
