"""add agent write intents

Revision ID: ae45f067cd89
Revises: ad34ef56bc78
Create Date: 2026-05-08 20:55:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "ae45f067cd89"
down_revision: str | None = "ad34ef56bc78"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_agent_write_intents",
        sa.Column("record_id", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.Column("trace_id", sa.String(), nullable=False),
        sa.Column("intent", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("authz_action", sa.String(), nullable=False),
        sa.Column("product", sa.String(), nullable=False),
        sa.Column("context", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("record_id"),
    )
    op.create_index(
        "launchplane_agent_write_intents_product_context_idx",
        "launchplane_agent_write_intents",
        ["product", "context", sa.text("recorded_at DESC")],
    )
    op.create_index(
        "launchplane_agent_write_intents_status_idx",
        "launchplane_agent_write_intents",
        ["status", sa.text("recorded_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_agent_write_intents_status_idx",
        table_name="launchplane_agent_write_intents",
    )
    op.drop_index(
        "launchplane_agent_write_intents_product_context_idx",
        table_name="launchplane_agent_write_intents",
    )
    op.drop_table("launchplane_agent_write_intents")
