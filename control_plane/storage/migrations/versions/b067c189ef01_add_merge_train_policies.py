"""add merge train policy records

Revision ID: b067c189ef01
Revises: af56b078de90
Create Date: 2026-05-13 21:10:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b067c189ef01"
down_revision: str | None = "af56b078de90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_merge_train_policies",
        sa.Column("record_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("policy_sha256", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("record_id"),
    )
    op.create_index(
        "launchplane_merge_train_policies_status_updated_idx",
        "launchplane_merge_train_policies",
        ["status", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_merge_train_policies_status_updated_idx",
        table_name="launchplane_merge_train_policies",
    )
    op.drop_table("launchplane_merge_train_policies")
