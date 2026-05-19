"""add generic web rollback plans

Revision ID: f6c7d8e9a0b1
Revises: f5b6c7d8e9a0
Create Date: 2026-05-19 00:35:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f6c7d8e9a0b1"
down_revision: str | None = "f5b6c7d8e9a0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_generic_web_rollback_plans"
_CONTEXT_INSTANCE_INDEX = "launchplane_generic_web_rollback_plans_context_instance_idx"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("plan_id", sa.String(), nullable=False),
        sa.Column("product", sa.String(), nullable=False),
        sa.Column("context", sa.String(), nullable=False),
        sa.Column("instance", sa.String(), nullable=False),
        sa.Column("created_at", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("rollback_deployment_record_id", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("plan_id"),
    )
    op.create_index(
        _CONTEXT_INSTANCE_INDEX,
        _TABLE,
        ["context", "instance", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(_CONTEXT_INSTANCE_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
