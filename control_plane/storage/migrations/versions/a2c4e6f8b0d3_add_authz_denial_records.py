"""add authz denial records

Revision ID: a2c4e6f8b0d3
Revises: f0a2c4e6b8d1
Create Date: 2026-08-18 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a2c4e6f8b0d3"
down_revision: str | None = "f0a2c4e6b8d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "launchplane_authz_denials" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "launchplane_authz_denials",
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.Column("expires_at", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("trace_id"),
    )
    op.create_index(
        "launchplane_authz_denials_recorded_idx",
        "launchplane_authz_denials",
        [sa.text("recorded_at DESC")],
    )
    op.create_index(
        "launchplane_authz_denials_expires_idx",
        "launchplane_authz_denials",
        ["expires_at"],
    )


def downgrade() -> None:
    if "launchplane_authz_denials" not in sa.inspect(op.get_bind()).get_table_names():
        return
    op.drop_index(
        "launchplane_authz_denials_expires_idx",
        table_name="launchplane_authz_denials",
    )
    op.drop_index(
        "launchplane_authz_denials_recorded_idx",
        table_name="launchplane_authz_denials",
    )
    op.drop_table("launchplane_authz_denials")
