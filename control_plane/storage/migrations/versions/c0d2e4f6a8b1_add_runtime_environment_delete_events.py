"""add runtime environment delete events

Revision ID: c0d2e4f6a8b1
Revises: b9c1d3e5f7a9
Create Date: 2026-05-01 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "c0d2e4f6a8b1"
down_revision: str | None = "b9c1d3e5f7a9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_runtime_environment_delete_events",
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("context", sa.String(), nullable=False),
        sa.Column("instance", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_index(
        "launchplane_runtime_environment_delete_events_route_idx",
        "launchplane_runtime_environment_delete_events",
        ["scope", "context", "instance", sa.text("recorded_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_runtime_environment_delete_events_route_idx",
        table_name="launchplane_runtime_environment_delete_events",
    )
    op.drop_table("launchplane_runtime_environment_delete_events")
