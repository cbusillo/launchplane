"""add merge train run records

Revision ID: af56b078de90
Revises: ae45f067cd89
Create Date: 2026-05-09 02:05:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "af56b078de90"
down_revision: str | None = "ae45f067cd89"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_merge_train_runs",
        sa.Column("run_id", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("base_branch", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("intended_next_action", sa.String(), nullable=False),
        sa.Column("selected_pr_number", sa.Integer(), nullable=True),
        sa.Column("policy_key", sa.String(), nullable=False),
        sa.Column("policy_sha256", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("run_id"),
    )
    op.create_index(
        "launchplane_merge_train_runs_repository_base_idx",
        "launchplane_merge_train_runs",
        ["repository", "base_branch", sa.text("recorded_at DESC")],
    )
    op.create_index(
        "launchplane_merge_train_runs_status_idx",
        "launchplane_merge_train_runs",
        ["status", sa.text("recorded_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_merge_train_runs_status_idx",
        table_name="launchplane_merge_train_runs",
    )
    op.drop_index(
        "launchplane_merge_train_runs_repository_base_idx",
        table_name="launchplane_merge_train_runs",
    )
    op.drop_table("launchplane_merge_train_runs")
