"""add preview inventory scans

Revision ID: b3c5d7e9f1a2
Revises: a2b4c6d8e9f0
Create Date: 2026-04-29 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b3c5d7e9f1a2"
down_revision: str | None = "a2b4c6d8e9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_preview_inventory_scans",
        sa.Column("scan_id", sa.String(), nullable=False),
        sa.Column("context", sa.String(), nullable=False),
        sa.Column("scanned_at", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("preview_count", sa.Integer(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("scan_id"),
    )
    op.create_index(
        "launchplane_preview_inventory_scans_context_idx",
        "launchplane_preview_inventory_scans",
        ["context", sa.text("scanned_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_preview_inventory_scans_context_idx",
        table_name="launchplane_preview_inventory_scans",
    )
    op.drop_table("launchplane_preview_inventory_scans")
