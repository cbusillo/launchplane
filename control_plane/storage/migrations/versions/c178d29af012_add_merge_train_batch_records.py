"""add merge train batch records

Revision ID: c178d29af012
Revises: b067c189ef01
Create Date: 2026-05-13 23:50:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c178d29af012"
down_revision: str | None = "b067c189ef01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_merge_train_batch_candidates",
        sa.Column("record_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("base_branch", sa.String(), nullable=False),
        sa.Column("batch_id", sa.String(), nullable=False),
        sa.Column("candidate_status", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("record_id"),
    )
    op.create_index(
        "launchplane_merge_train_batch_candidates_repository_base_idx",
        "launchplane_merge_train_batch_candidates",
        ["repository", "base_branch", sa.text("updated_at DESC")],
    )
    op.create_index(
        "launchplane_merge_train_batch_candidates_status_idx",
        "launchplane_merge_train_batch_candidates",
        ["status", sa.text("updated_at DESC")],
    )
    op.create_table(
        "launchplane_merge_train_batch_landing_plans",
        sa.Column("record_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("base_branch", sa.String(), nullable=False),
        sa.Column("batch_id", sa.String(), nullable=False),
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("record_id"),
    )
    op.create_index(
        "launchplane_merge_train_batch_landing_plans_repository_base_idx",
        "launchplane_merge_train_batch_landing_plans",
        ["repository", "base_branch", sa.text("updated_at DESC")],
    )
    op.create_index(
        "launchplane_merge_train_batch_landing_plans_status_idx",
        "launchplane_merge_train_batch_landing_plans",
        ["status", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_merge_train_batch_landing_plans_status_idx",
        table_name="launchplane_merge_train_batch_landing_plans",
    )
    op.drop_index(
        "launchplane_merge_train_batch_landing_plans_repository_base_idx",
        table_name="launchplane_merge_train_batch_landing_plans",
    )
    op.drop_table("launchplane_merge_train_batch_landing_plans")
    op.drop_index(
        "launchplane_merge_train_batch_candidates_status_idx",
        table_name="launchplane_merge_train_batch_candidates",
    )
    op.drop_index(
        "launchplane_merge_train_batch_candidates_repository_base_idx",
        table_name="launchplane_merge_train_batch_candidates",
    )
    op.drop_table("launchplane_merge_train_batch_candidates")
