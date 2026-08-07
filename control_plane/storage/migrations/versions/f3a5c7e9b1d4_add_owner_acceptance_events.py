"""add owner acceptance events

Revision ID: f3a5c7e9b1d4
Revises: f4a6c8e0b2d4
Create Date: 2026-08-07 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f3a5c7e9b1d4"
down_revision: str | None = "f4a6c8e0b2d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_owner_acceptance_events"


def _table_exists() -> bool:
    return _TABLE in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists(index_name: str) -> bool:
    if not _table_exists():
        return False
    return index_name in {
        str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
    }


def upgrade() -> None:
    if not _table_exists():
        op.create_table(
            _TABLE,
            sa.Column("event_id", sa.String(), nullable=False),
            sa.Column("acceptance_id", sa.String(), nullable=False),
            sa.Column("binding_sha256", sa.String(), nullable=False),
            sa.Column("repository_id", sa.String(), nullable=False),
            sa.Column("repository_owner_id", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("pr_number", sa.BigInteger(), nullable=False),
            sa.Column("head_sha", sa.String(), nullable=False),
            sa.Column("tree_sha", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("system", sa.String(), nullable=False),
            sa.Column("owner_action", sa.String(), nullable=False),
            sa.Column("environment", sa.String(), nullable=False),
            sa.Column("action", sa.String(), nullable=False),
            sa.Column("owner_github_id", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("owner_login", sa.String(), nullable=False, server_default=""),
            sa.Column("occurred_at", sa.String(), nullable=False),
            sa.Column(
                "payload",
                sa.JSON().with_variant(
                    postgresql.JSONB(astext_type=sa.Text()),
                    "postgresql",
                ),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("event_id"),
        )
    subject_index = "launchplane_owner_acceptance_events_subject_idx"
    if not _index_exists(subject_index):
        op.create_index(
            subject_index,
            _TABLE,
            [
                "repository_id",
                "pr_number",
                "product",
                "system",
                "owner_action",
                sa.text("occurred_at DESC"),
            ],
        )
    binding_index = "launchplane_owner_acceptance_events_binding_idx"
    if not _index_exists(binding_index):
        op.create_index(
            binding_index,
            _TABLE,
            ["binding_sha256", sa.text("occurred_at DESC")],
        )
    acceptance_index = "launchplane_owner_acceptance_events_acceptance_idx"
    if not _index_exists(acceptance_index):
        op.create_index(
            acceptance_index,
            _TABLE,
            ["acceptance_id", sa.text("occurred_at DESC")],
        )


def downgrade() -> None:
    if not _table_exists():
        return
    for index_name in (
        "launchplane_owner_acceptance_events_acceptance_idx",
        "launchplane_owner_acceptance_events_binding_idx",
        "launchplane_owner_acceptance_events_subject_idx",
    ):
        if _index_exists(index_name):
            op.drop_index(index_name, table_name=_TABLE)
    op.drop_table(_TABLE)
