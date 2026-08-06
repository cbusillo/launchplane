"""add engineering review runs

Revision ID: d2e4f6a8c0b2
Revises: c1d2e3f4a5b6
Create Date: 2026-08-05 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "d2e4f6a8c0b2"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_engineering_review_runs"
_PR_IDX = "launchplane_eng_review_runs_pr_idx"
_WORK_REQUEST_IDX = "launchplane_eng_review_runs_work_request_idx"
_STATE_LEASE_IDX = "launchplane_eng_review_runs_state_lease_idx"


def _table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return table_name in set(inspector.get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return index_name in {str(idx["name"]) for idx in inspector.get_indexes(table_name)}


def _payload_column() -> sa.Column[object]:
    return sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        nullable=False,
    )


def upgrade() -> None:
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("run_id", sa.String(), nullable=False),
            sa.Column("review_slot", sa.Integer(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("pr_number", sa.Integer(), nullable=False),
            sa.Column("head_sha", sa.String(), nullable=False),
            sa.Column("work_request_id", sa.String(), nullable=False),
            sa.Column("policy_revision", sa.String(), nullable=False),
            sa.Column("lease_expires_at", sa.String(), nullable=False, server_default=""),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            _payload_column(),
            sa.PrimaryKeyConstraint("run_id"),
        )
    if not _index_exists(_TABLE, _PR_IDX):
        op.create_index(
            _PR_IDX,
            _TABLE,
            ["repository", "pr_number", sa.text("created_at DESC")],
        )
    if not _index_exists(_TABLE, _WORK_REQUEST_IDX):
        op.create_index(
            _WORK_REQUEST_IDX,
            _TABLE,
            ["work_request_id", sa.text("created_at DESC")],
        )
    if not _index_exists(_TABLE, _STATE_LEASE_IDX):
        op.create_index(
            _STATE_LEASE_IDX,
            _TABLE,
            ["state", "lease_expires_at"],
        )


def downgrade() -> None:
    if _table_exists(_TABLE):
        if _index_exists(_TABLE, _STATE_LEASE_IDX):
            op.drop_index(_STATE_LEASE_IDX, table_name=_TABLE)
        if _index_exists(_TABLE, _WORK_REQUEST_IDX):
            op.drop_index(_WORK_REQUEST_IDX, table_name=_TABLE)
        if _index_exists(_TABLE, _PR_IDX):
            op.drop_index(_PR_IDX, table_name=_TABLE)
        op.drop_table(_TABLE)
