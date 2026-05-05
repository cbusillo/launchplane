"""add runtime key safety policies

Revision ID: f1a3c5e7b9d0
Revises: c0d2e4f6a8b1
Create Date: 2026-05-05 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f1a3c5e7b9d0"
down_revision: str | None = "c0d2e4f6a8b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_runtime_key_safety_policies",
        sa.Column("record_id", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("record_id"),
    )
    op.create_index(
        "launchplane_runtime_key_safety_policies_updated_idx",
        "launchplane_runtime_key_safety_policies",
        [sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_runtime_key_safety_policies_updated_idx",
        table_name="launchplane_runtime_key_safety_policies",
    )
    op.drop_table("launchplane_runtime_key_safety_policies")
