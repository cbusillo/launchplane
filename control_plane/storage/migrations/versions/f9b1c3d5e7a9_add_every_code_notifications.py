"""add every code notifications

Revision ID: f9b1c3d5e7a9
Revises: c8a0b2c4d6e8
Create Date: 2026-06-14 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f9b1c3d5e7a9"
down_revision: str | None = "c8a0b2c4d6e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICIES_TABLE = "launchplane_every_code_notification_policies"
_ATTEMPTS_TABLE = "launchplane_every_code_notification_attempts"
_POLICIES_SCOPE_INDEX = "launchplane_every_code_notify_policies_scope_idx"
_ATTEMPTS_REQUEST_INDEX = "launchplane_every_code_notify_attempts_request_idx"
_ATTEMPTS_DESTINATION_INDEX = "launchplane_every_code_notify_attempts_destination_idx"


def _table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return table_name in set(inspector.get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return index_name in {str(index["name"]) for index in inspector.get_indexes(table_name)}


def _payload_column() -> sa.Column[object]:
    return sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        nullable=False,
    )


def upgrade() -> None:
    if not _table_exists(_POLICIES_TABLE):
        op.create_table(
            _POLICIES_TABLE,
            sa.Column("policy_id", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            _payload_column(),
            sa.PrimaryKeyConstraint("policy_id"),
        )
    if not _index_exists(_POLICIES_TABLE, _POLICIES_SCOPE_INDEX):
        op.create_index(
            _POLICIES_SCOPE_INDEX,
            _POLICIES_TABLE,
            ["repository", "status", sa.text("updated_at DESC")],
        )

    if not _table_exists(_ATTEMPTS_TABLE):
        op.create_table(
            _ATTEMPTS_TABLE,
            sa.Column("attempt_id", sa.String(), nullable=False),
            sa.Column("request_id", sa.String(), nullable=False),
            sa.Column("event", sa.String(), nullable=False),
            sa.Column("destination_kind", sa.String(), nullable=False),
            sa.Column("delivery_status", sa.String(), nullable=False),
            sa.Column("attempted_at", sa.String(), nullable=False),
            _payload_column(),
            sa.PrimaryKeyConstraint("attempt_id"),
        )
    if not _index_exists(_ATTEMPTS_TABLE, _ATTEMPTS_REQUEST_INDEX):
        op.create_index(
            _ATTEMPTS_REQUEST_INDEX,
            _ATTEMPTS_TABLE,
            ["request_id", "event", sa.text("attempted_at DESC")],
        )
    if not _index_exists(_ATTEMPTS_TABLE, _ATTEMPTS_DESTINATION_INDEX):
        op.create_index(
            _ATTEMPTS_DESTINATION_INDEX,
            _ATTEMPTS_TABLE,
            ["destination_kind", sa.text("attempted_at DESC")],
        )


def downgrade() -> None:
    if _table_exists(_ATTEMPTS_TABLE):
        if _index_exists(_ATTEMPTS_TABLE, _ATTEMPTS_DESTINATION_INDEX):
            op.drop_index(_ATTEMPTS_DESTINATION_INDEX, table_name=_ATTEMPTS_TABLE)
        if _index_exists(_ATTEMPTS_TABLE, _ATTEMPTS_REQUEST_INDEX):
            op.drop_index(_ATTEMPTS_REQUEST_INDEX, table_name=_ATTEMPTS_TABLE)
        op.drop_table(_ATTEMPTS_TABLE)
    if _table_exists(_POLICIES_TABLE):
        if _index_exists(_POLICIES_TABLE, _POLICIES_SCOPE_INDEX):
            op.drop_index(_POLICIES_SCOPE_INDEX, table_name=_POLICIES_TABLE)
        op.drop_table(_POLICIES_TABLE)
