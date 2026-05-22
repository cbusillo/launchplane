"""add preview enablement records

Revision ID: b1c3d5e7f9a1
Revises: f7d8e9a0b1c2
Create Date: 2026-05-22 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b1c3d5e7f9a1"
down_revision: str | None = "f7d8e9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_preview_enablement",
        sa.Column("record_id", sa.String(), nullable=False),
        sa.Column("context", sa.String(), nullable=False),
        sa.Column("anchor_repo", sa.String(), nullable=False),
        sa.Column("anchor_pr_number", sa.Integer(), nullable=False),
        sa.Column("pr_state", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("record_id"),
    )
    op.create_index(
        "launchplane_preview_enablement_context_idx",
        "launchplane_preview_enablement",
        ["context", sa.text("updated_at DESC")],
    )
    op.create_index(
        "launchplane_preview_enablement_anchor_idx",
        "launchplane_preview_enablement",
        ["anchor_repo", "anchor_pr_number", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_preview_enablement_anchor_idx",
        table_name="launchplane_preview_enablement",
    )
    op.drop_index(
        "launchplane_preview_enablement_context_idx",
        table_name="launchplane_preview_enablement",
    )
    op.drop_table("launchplane_preview_enablement")
