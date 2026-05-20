"""add merge train PR feedback records

Revision ID: f7d8e9a0b1c2
Revises: f6c7d8e9a0b1
Create Date: 2026-05-20 14:30:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f7d8e9a0b1c2"
down_revision: str | None = "f6c7d8e9a0b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_merge_train_pr_feedback"
_PR_INDEX = "launchplane_merge_train_pr_feedback_pr_idx"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("feedback_id", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("base_branch", sa.String(), nullable=False),
        sa.Column("pull_request_number", sa.Integer(), nullable=False),
        sa.Column("event", sa.String(), nullable=False),
        sa.Column("delivery_status", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("feedback_id"),
    )
    op.create_index(
        _PR_INDEX,
        _TABLE,
        ["repository", "base_branch", "pull_request_number", sa.text("recorded_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(_PR_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
