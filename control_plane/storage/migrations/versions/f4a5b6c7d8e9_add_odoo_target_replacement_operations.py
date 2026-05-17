"""add Odoo target replacement operations

Revision ID: f4a5b6c7d8e9
Revises: e39f4a5b6c7d
Create Date: 2026-05-17 17:05:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f4a5b6c7d8e9"
down_revision: str | None = "e39f4a5b6c7d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_odoo_stable_target_replacement_operations",
        sa.Column("operation_id", sa.String(), nullable=False),
        sa.Column("product", sa.String(), nullable=False),
        sa.Column("context", sa.String(), nullable=False),
        sa.Column("instance", sa.String(), nullable=False),
        sa.Column("idempotency_key", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("phase", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("operation_id"),
    )
    op.create_index(
        "launchplane_odoo_replacement_operation_lane_status_idx",
        "launchplane_odoo_stable_target_replacement_operations",
        ["product", "context", "instance", "status", sa.text("updated_at DESC")],
    )
    op.create_index(
        "launchplane_odoo_replacement_operation_idempotency_idx",
        "launchplane_odoo_stable_target_replacement_operations",
        ["idempotency_key", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_odoo_replacement_operation_idempotency_idx",
        table_name="launchplane_odoo_stable_target_replacement_operations",
    )
    op.drop_index(
        "launchplane_odoo_replacement_operation_lane_status_idx",
        table_name="launchplane_odoo_stable_target_replacement_operations",
    )
    op.drop_table("launchplane_odoo_stable_target_replacement_operations")
