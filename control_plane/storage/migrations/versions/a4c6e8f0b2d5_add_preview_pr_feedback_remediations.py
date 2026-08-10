"""add preview PR feedback remediations

Revision ID: a4c6e8f0b2d5
Revises: f3a5c7e9b1d4
Create Date: 2026-08-10 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a4c6e8f0b2d5"
down_revision: str | None = "f3a5c7e9b1d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_preview_pr_feedback_remediations"


def _table_exists() -> bool:
    return _TABLE in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists(index_name: str) -> bool:
    if not _table_exists():
        return False
    return index_name in {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
    }


def upgrade() -> None:
    if not _table_exists():
        op.create_table(
            _TABLE,
            sa.Column("remediation_id", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("pull_request_number", sa.Integer(), nullable=False),
            sa.Column("actor", sa.String(), nullable=False),
            sa.Column("idempotency_key", sa.String(), nullable=False),
            sa.Column("requested_at", sa.String(), nullable=False),
            sa.Column("mode", sa.String(), nullable=False),
            sa.Column("outcome", sa.String(), nullable=False),
            sa.Column(
                "payload",
                sa.JSON().with_variant(
                    postgresql.JSONB(astext_type=sa.Text()),
                    "postgresql",
                ),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("remediation_id"),
        )
    target_index = "launchplane_preview_pr_feedback_remediations_target_idx"
    if not _index_exists(target_index):
        op.create_index(
            target_index,
            _TABLE,
            ["repository", "pull_request_number", sa.text("requested_at DESC")],
        )
    idempotency_index = "launchplane_preview_pr_feedback_remediations_idempotency_idx"
    if not _index_exists(idempotency_index):
        op.create_index(
            idempotency_index,
            _TABLE,
            ["actor", "idempotency_key", sa.text("requested_at DESC")],
        )


def downgrade() -> None:
    if not _table_exists():
        return
    op.drop_index(
        "launchplane_preview_pr_feedback_remediations_idempotency_idx",
        table_name=_TABLE,
    )
    op.drop_index(
        "launchplane_preview_pr_feedback_remediations_target_idx",
        table_name=_TABLE,
    )
    op.drop_table(_TABLE)
