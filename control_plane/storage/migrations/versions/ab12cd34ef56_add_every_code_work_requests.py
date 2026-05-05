"""add Every Code work requests

Revision ID: ab12cd34ef56
Revises: f1a3c5e7b9d0
Create Date: 2026-05-05 22:40:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "ab12cd34ef56"
down_revision: str | None = "f1a3c5e7b9d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_every_code_work_requests",
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("trigger_label", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column("claimed_by_host", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("request_id"),
    )
    op.create_index(
        "launchplane_every_code_work_requests_state_updated_idx",
        "launchplane_every_code_work_requests",
        ["state", sa.text("updated_at DESC")],
    )
    op.create_index(
        "launchplane_every_code_work_requests_repo_issue_idx",
        "launchplane_every_code_work_requests",
        ["repository", "issue_number"],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_every_code_work_requests_repo_issue_idx",
        table_name="launchplane_every_code_work_requests",
    )
    op.drop_index(
        "launchplane_every_code_work_requests_state_updated_idx",
        table_name="launchplane_every_code_work_requests",
    )
    op.drop_table("launchplane_every_code_work_requests")
