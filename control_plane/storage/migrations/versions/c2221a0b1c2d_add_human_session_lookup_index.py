"""add human session lookup index

Revision ID: c2221a0b1c2d
Revises: b4d6f8a0c2e5
Create Date: 2026-08-23 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op


revision: str = "c2221a0b1c2d"
down_revision: str | None = "b4d6f8a0c2e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "launchplane_human_sessions_github_id_idx",
        "launchplane_human_sessions",
        ["github_id"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_human_sessions_github_id_idx",
        table_name="launchplane_human_sessions",
        if_exists=True,
    )
