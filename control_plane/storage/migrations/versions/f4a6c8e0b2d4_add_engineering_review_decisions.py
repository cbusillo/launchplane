"""add engineering review decisions

Revision ID: f4a6c8e0b2d4
Revises: e3f5a7b9c1d3
Create Date: 2026-08-06 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f4a6c8e0b2d4"
down_revision: str | None = "e3f5a7b9c1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_engineering_review_decisions"


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("decision_id", sa.String(), primary_key=True),
            sa.Column("decision_binding_sha256", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("pull_request_number", sa.Integer(), nullable=False),
            sa.Column("head_sha", sa.String(), nullable=False),
            sa.Column("tree_sha", sa.String(), nullable=False),
            sa.Column("work_request_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("evaluated_at", sa.String(), nullable=False),
            sa.Column(
                "payload",
                postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite"),
                nullable=False,
            ),
        )
    indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(_TABLE)}
    if "launchplane_eng_review_decisions_target_idx" not in indexes:
        op.create_index(
            "launchplane_eng_review_decisions_target_idx",
            _TABLE,
            ["repository", "pull_request_number", "head_sha", sa.text("evaluated_at DESC")],
        )
    if "launchplane_eng_review_decisions_work_request_idx" not in indexes:
        op.create_index(
            "launchplane_eng_review_decisions_work_request_idx",
            _TABLE,
            ["work_request_id", sa.text("evaluated_at DESC")],
        )
    if "launchplane_eng_review_decisions_binding_uidx" not in indexes:
        op.create_index(
            "launchplane_eng_review_decisions_binding_uidx",
            _TABLE,
            ["decision_binding_sha256"],
            unique=True,
        )


def downgrade() -> None:
    if _table_exists(_TABLE):
        op.drop_table(_TABLE)
