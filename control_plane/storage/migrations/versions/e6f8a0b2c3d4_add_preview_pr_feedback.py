"""add preview pr feedback

Revision ID: e6f8a0b2c3d4
Revises: d5e7f9a1b2c3
Create Date: 2026-04-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e6f8a0b2c3d4"
down_revision: str | None = "d5e7f9a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_preview_pr_feedback",
        sa.Column("feedback_id", sa.String(), nullable=False),
        sa.Column("product", sa.String(), nullable=False),
        sa.Column("context", sa.String(), nullable=False),
        sa.Column("anchor_repo", sa.String(), nullable=False),
        sa.Column("anchor_pr_number", sa.Integer(), nullable=False),
        sa.Column("requested_at", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("delivery_status", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("feedback_id"),
    )
    op.create_index(
        "launchplane_preview_pr_feedback_context_idx",
        "launchplane_preview_pr_feedback",
        ["context", sa.text("requested_at DESC")],
    )
    op.create_index(
        "launchplane_preview_pr_feedback_anchor_idx",
        "launchplane_preview_pr_feedback",
        ["anchor_repo", "anchor_pr_number", sa.text("requested_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_preview_pr_feedback_anchor_idx",
        table_name="launchplane_preview_pr_feedback",
    )
    op.drop_index(
        "launchplane_preview_pr_feedback_context_idx",
        table_name="launchplane_preview_pr_feedback",
    )
    op.drop_table("launchplane_preview_pr_feedback")
