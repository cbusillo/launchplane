"""add product profiles

Revision ID: b9c1d3e5f7a9
Revises: a8b0c2d4e6f8
Create Date: 2026-04-30 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b9c1d3e5f7a9"
down_revision: str | None = "a8b0c2d4e6f8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_product_profiles",
        sa.Column("product", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("driver_id", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("product"),
    )
    op.create_index(
        "launchplane_product_profiles_driver_idx",
        "launchplane_product_profiles",
        ["driver_id", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("launchplane_product_profiles_driver_idx", table_name="launchplane_product_profiles")
    op.drop_table("launchplane_product_profiles")
