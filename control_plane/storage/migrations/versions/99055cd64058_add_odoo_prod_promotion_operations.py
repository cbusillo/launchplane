"""Add durable Odoo prod promotion operations.

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

_TABLE = "launchplane_odoo_prod_promotion_operations"
_LANE_STATUS_INDEX = "launchplane_odoo_promotion_operation_lane_status_idx"
_ACTIVE_LANE_INDEX = "launchplane_odoo_promotion_active_lane_uidx"
_WORKER_CLAIM_INDEX = "launchplane_odoo_promotion_worker_claim_idx"
_ACTIVE_LANE_PREDICATE = "status IN ('pending', 'running', 'reconciliation_required')"


def upgrade() -> None:
    if _TABLE in set(sa.inspect(op.get_bind()).get_table_names()):
        return
    op.create_table(
        _TABLE,
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
        _LANE_STATUS_INDEX,
        _TABLE,
        ["product", "context", "instance", "status", sa.text("updated_at DESC")],
    )
    op.create_index(
        _ACTIVE_LANE_INDEX,
        _TABLE,
        ["product", "context", "instance"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_LANE_PREDICATE),
        sqlite_where=sa.text(_ACTIVE_LANE_PREDICATE),
    )
    op.create_index(_WORKER_CLAIM_INDEX, _TABLE, ["status", "lease_expires_at", "updated_at"])


def downgrade() -> None:
    op.drop_index(_WORKER_CLAIM_INDEX, table_name=_TABLE)
    op.drop_index(_ACTIVE_LANE_INDEX, table_name=_TABLE)
    op.drop_index(_LANE_STATUS_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
