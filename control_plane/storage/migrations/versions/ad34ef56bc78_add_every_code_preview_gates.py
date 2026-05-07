"""add Every Code preview gates

Revision ID: ad34ef56bc78
Revises: ac23de45fa67
Create Date: 2026-05-07 16:30:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "ad34ef56bc78"
down_revision: str | None = "ac23de45fa67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "launchplane_every_code_preview_gates",
        sa.Column("gate_id", sa.String(), nullable=False),
        sa.Column("request_id", sa.String(), nullable=False),
        sa.Column("repository", sa.String(), nullable=False),
        sa.Column("issue_number", sa.Integer(), nullable=False),
        sa.Column("pr_number", sa.Integer(), nullable=False),
        sa.Column("head_sha", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("updated_at", sa.String(), nullable=False),
        sa.Column(
            "payload",
            sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("gate_id"),
    )
    op.create_index(
        "launchplane_every_code_preview_gates_request_idx",
        "launchplane_every_code_preview_gates",
        ["request_id", sa.text("updated_at DESC")],
    )
    op.create_index(
        "launchplane_every_code_preview_gates_pr_idx",
        "launchplane_every_code_preview_gates",
        ["repository", "pr_number", sa.text("updated_at DESC")],
    )
    op.create_index(
        "launchplane_every_code_preview_gates_status_idx",
        "launchplane_every_code_preview_gates",
        ["status", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "launchplane_every_code_preview_gates_status_idx",
        table_name="launchplane_every_code_preview_gates",
    )
    op.drop_index(
        "launchplane_every_code_preview_gates_pr_idx",
        table_name="launchplane_every_code_preview_gates",
    )
    op.drop_index(
        "launchplane_every_code_preview_gates_request_idx",
        table_name="launchplane_every_code_preview_gates",
    )
    op.drop_table("launchplane_every_code_preview_gates")
