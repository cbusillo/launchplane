"""add merge train stack collapse plans

Revision ID: d289e3ab4012
Revises: c178d29af012
Create Date: 2026-05-14 13:35:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d289e3ab4012"
down_revision: str | None = "c178d29af012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_merge_train_stack_collapse_plans",
        sa.Column("record_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("base_branch", sa.String(), nullable=False),
        sa.Column("collapse_id", sa.String(), nullable=False),
        sa.Column("root_pull_request_number", sa.Integer(), nullable=False),
        sa.Column("plan_status", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("record_id"),
    )
    op.create_index(
        "launchplane_merge_train_stack_collapse_repository_base_idx",
        "launchplane_merge_train_stack_collapse_plans",
        ["repository", "base_branch", sa.text("updated_at DESC")],
    )
    op.create_index(
        "launchplane_merge_train_stack_collapse_plans_status_idx",
        "launchplane_merge_train_stack_collapse_plans",
        ["status", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_merge_train_stack_collapse_plans_status_idx",
        table_name="launchplane_merge_train_stack_collapse_plans",
    )
    op.drop_index(
        "launchplane_merge_train_stack_collapse_repository_base_idx",
        table_name="launchplane_merge_train_stack_collapse_plans",
    )
    op.drop_table("launchplane_merge_train_stack_collapse_plans")
