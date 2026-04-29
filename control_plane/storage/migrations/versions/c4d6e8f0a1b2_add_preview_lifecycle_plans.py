"""add preview lifecycle plans

Revision ID: c4d6e8f0a1b2
Revises: b3c5d7e9f1a2
Create Date: 2026-04-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c4d6e8f0a1b2"
down_revision: str | None = "b3c5d7e9f1a2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_preview_lifecycle_plans",
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("product", sa.String(), nullable=False),
        sa.Column("context", sa.String(), nullable=False),
        sa.Column("planned_at", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("inventory_scan_id", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("plan_id"),
    )
    op.create_index(
        "launchplane_preview_lifecycle_plans_context_idx",
        "launchplane_preview_lifecycle_plans",
        ["context", sa.text("planned_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_preview_lifecycle_plans_context_idx",
        table_name="launchplane_preview_lifecycle_plans",
    )
    op.drop_table("launchplane_preview_lifecycle_plans")
