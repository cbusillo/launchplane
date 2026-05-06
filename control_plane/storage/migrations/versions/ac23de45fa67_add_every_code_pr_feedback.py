"""add Every Code PR feedback

Revision ID: ac23de45fa67
Revises: ab12cd34ef56
Create Date: 2026-05-06 18:40:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "ac23de45fa67"
down_revision: str | None = "ab12cd34ef56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_every_code_pr_feedback",
        sa.Column("feedback_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("feedback_kind", sa.String(), nullable=False),
        sa.Column("github_delivery_id", sa.String(), nullable=False),
        sa.Column("actor", sa.String(), nullable=False),
        sa.Column("received_at", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("feedback_id"),
    )
    op.create_index(
        "launchplane_every_code_pr_feedback_request_idx",
        "launchplane_every_code_pr_feedback",
        ["request_id", sa.text("received_at DESC")],
    )
    op.create_index(
        "launchplane_every_code_pr_feedback_pr_idx",
        "launchplane_every_code_pr_feedback",
        ["repository", "pr_number", sa.text("received_at DESC")],
    )
    op.create_index(
        "launchplane_every_code_pr_feedback_status_idx",
        "launchplane_every_code_pr_feedback",
        ["status", sa.text("received_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_every_code_pr_feedback_status_idx",
        table_name="launchplane_every_code_pr_feedback",
    )
    op.drop_index(
        "launchplane_every_code_pr_feedback_pr_idx",
        table_name="launchplane_every_code_pr_feedback",
    )
    op.drop_index(
        "launchplane_every_code_pr_feedback_request_idx",
        table_name="launchplane_every_code_pr_feedback",
    )
    op.drop_table("launchplane_every_code_pr_feedback")
