"""add preview lifecycle cleanups

Revision ID: d5e7f9a1b2c3
Revises: c4d6e8f0a1b2
Create Date: 2026-04-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d5e7f9a1b2c3"
down_revision: str | None = "c4d6e8f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_preview_lifecycle_cleanups",
        sa.Column("cleanup_id", sa.String(), nullable=False),
        sa.Column("product", sa.String(), nullable=False),
        sa.Column("context", sa.String(), nullable=False),
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("requested_at", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("cleanup_id"),
    )
    op.create_index(
        "launchplane_preview_lifecycle_cleanups_context_idx",
        "launchplane_preview_lifecycle_cleanups",
        ["context", sa.text("requested_at DESC")],
    )
    op.create_index(
        "launchplane_preview_lifecycle_cleanups_plan_idx",
        "launchplane_preview_lifecycle_cleanups",
        ["plan_id", sa.text("requested_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_preview_lifecycle_cleanups_plan_idx",
        table_name="launchplane_preview_lifecycle_cleanups",
    )
    op.drop_index(
        "launchplane_preview_lifecycle_cleanups_context_idx",
        table_name="launchplane_preview_lifecycle_cleanups",
    )
    op.drop_table("launchplane_preview_lifecycle_cleanups")
