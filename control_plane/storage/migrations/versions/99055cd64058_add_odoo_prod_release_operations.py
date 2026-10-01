"""Add durable Odoo prod promotion and rollback operations.

Revision ID: 99055cd64058
Revises: 228e144bb12d
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "99055cd64058"
down_revision: str | None = "228e144bb12d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# (table, index-name prefix) for each queued Odoo release operation kind.
_TABLES = (
    ("launchplane_odoo_prod_promotion_operations", "launchplane_odoo_promotion"),
    ("launchplane_odoo_prod_rollback_operations", "launchplane_odoo_rollback"),
)
_ACTIVE_LANE_PREDICATE = "status IN ('pending', 'running', 'reconciliation_required')"


def upgrade() -> None:
    existing_tables = set(sa.inspect(op.get_bind()).get_table_names())
    for table, prefix in _TABLES:
        if table in existing_tables:
            continue
        op.create_table(
            table,
            sa.Column("operation_id", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("instance", sa.String(), nullable=False),
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
        op.create_index(
            f"{prefix}_operation_lane_status_idx",
            table,
            ["product", "context", "instance", "status", sa.text("updated_at DESC")],
        )
        op.create_index(
            f"{prefix}_active_lane_uidx",
            table,
            ["product", "context", "instance"],
            unique=True,
            postgresql_where=sa.text(_ACTIVE_LANE_PREDICATE),
            sqlite_where=sa.text(_ACTIVE_LANE_PREDICATE),
        )
        op.create_index(
            f"{prefix}_worker_claim_idx", table, ["status", "lease_expires_at", "updated_at"]
        )


def downgrade() -> None:
    for table, prefix in reversed(_TABLES):
        op.drop_index(f"{prefix}_worker_claim_idx", table_name=table)
        op.drop_index(f"{prefix}_active_lane_uidx", table_name=table)
        op.drop_index(f"{prefix}_operation_lane_status_idx", table_name=table)
        op.drop_table(table)
