"""add preview desired states

Revision ID: f7a9b1c2d3e4
Revises: e6f8a0b2c3d4
Create Date: 2026-04-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f7a9b1c2d3e4"
down_revision: str | None = "e6f8a0b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_preview_desired_states",
        sa.Column("desired_state_id", sa.String(), nullable=False),
        sa.Column("product", sa.String(), nullable=False),
        sa.Column("context", sa.String(), nullable=False),
        sa.Column("discovered_at", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("desired_count", sa.Integer(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("desired_state_id"),
    )
    op.create_index(
        "launchplane_preview_desired_states_context_idx",
        "launchplane_preview_desired_states",
        ["context", sa.text("discovered_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_preview_desired_states_context_idx",
        table_name="launchplane_preview_desired_states",
    )
    op.drop_table("launchplane_preview_desired_states")
