"""add authz policy records

Revision ID: a8b0c2d4e6f8
Revises: f7a9b1c2d3e4
Create Date: 2026-04-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a8b0c2d4e6f8"
down_revision: str | None = "f7a9b1c2d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_authz_policies",
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
        "launchplane_authz_policies_updated_idx",
        "launchplane_authz_policies",
        [sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("launchplane_authz_policies_updated_idx", table_name="launchplane_authz_policies")
    op.drop_table("launchplane_authz_policies")

