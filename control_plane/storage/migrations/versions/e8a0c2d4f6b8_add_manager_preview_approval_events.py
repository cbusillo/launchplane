"""add manager preview approval events

Revision ID: e8a0c2d4f6b8
Revises: d7f9a1b3c5e7
Create Date: 2026-07-30 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "e8a0c2d4f6b8"
down_revision: str | None = "d7f9a1b3c5e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_manager_preview_approval_events"


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
            sa.Column("approval_id", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("pr_number", sa.Integer(), nullable=False),
            sa.Column("head_sha", sa.String(), nullable=False),
            sa.Column("preview_id", sa.String(), nullable=False),
            sa.Column("serving_generation_id", sa.String(), nullable=False),
            sa.Column("artifact_id", sa.String(), nullable=False),
            sa.Column("artifact_image_digest", sa.String(), nullable=False),
            sa.Column("manifest_fingerprint", sa.String(), nullable=False),
            sa.Column("runtime_identity_sha256", sa.String(), nullable=False),
            sa.Column("action", sa.String(), nullable=False),
            sa.Column("manager_github_id", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("manager_login", sa.String(), nullable=False, server_default=""),
            sa.Column("policy_record_id", sa.String(), nullable=False, server_default=""),
            sa.Column("policy_sha256", sa.String(), nullable=False, server_default=""),
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
    subject_index = "launchplane_manager_preview_approval_events_subject_idx"
    if not _index_exists(subject_index):
        op.create_index(
            subject_index,
            _TABLE,
            ["product", "context", "repository", "pr_number", sa.text("occurred_at DESC")],
        )
    preview_index = "launchplane_manager_preview_approval_events_preview_idx"
    if not _index_exists(preview_index):
        op.create_index(
            preview_index,
            _TABLE,
            ["preview_id", "serving_generation_id", sa.text("occurred_at DESC")],
        )
    approval_index = "launchplane_manager_preview_approval_events_approval_idx"
    if not _index_exists(approval_index):
        op.create_index(
            approval_index,
            _TABLE,
            ["approval_id", sa.text("occurred_at DESC")],
        )


def downgrade() -> None:
    if not _table_exists():
        return
    for index_name in (
        "launchplane_manager_preview_approval_events_approval_idx",
        "launchplane_manager_preview_approval_events_preview_idx",
        "launchplane_manager_preview_approval_events_subject_idx",
    ):
        if _index_exists(index_name):
            op.drop_index(index_name, table_name=_TABLE)
    op.drop_table(_TABLE)
