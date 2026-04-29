"""add human sessions

Revision ID: a2b4c6d8e9f0
Revises: fe94a0486977
Create Date: 2026-04-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a2b4c6d8e9f0"
down_revision: str | None = "fe94a0486977"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_human_sessions",
        sa.Column("session_id", sa.String(), nullable=False),
        sa.Column("login", sa.String(), nullable=False),
        sa.Column("github_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        "launchplane_human_sessions_expires_idx",
        "launchplane_human_sessions",
        [sa.text("expires_at DESC")],
    )
    op.create_index(
        "launchplane_human_sessions_login_idx",
        "launchplane_human_sessions",
        ["login", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_human_sessions_login_idx",
        table_name="launchplane_human_sessions",
    )
    op.drop_index(
        "launchplane_human_sessions_expires_idx",
        table_name="launchplane_human_sessions",
    )
    op.drop_table("launchplane_human_sessions")
