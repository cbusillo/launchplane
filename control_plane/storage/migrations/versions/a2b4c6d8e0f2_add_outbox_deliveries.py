"""add outbox deliveries

Revision ID: a2b4c6d8e0f2
Revises: f2a4c6e8b0d2
Create Date: 2026-07-13 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a2b4c6d8e0f2"
down_revision: str | None = "f2a4c6e8b0d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_outbox_deliveries"
_DEDUPE_INDEX = "launchplane_outbox_deliveries_dedupe_uidx"
_CLAIM_INDEX = "launchplane_outbox_deliveries_claim_idx"
_AGGREGATE_INDEX = "launchplane_outbox_deliveries_aggregate_idx"
_PROVIDER_KEY_INDEX = "launchplane_outbox_deliveries_provider_key_idx"


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
    if not _table_exists(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("delivery_id", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("aggregate_type", sa.String(), nullable=False),
            sa.Column("aggregate_id", sa.String(), nullable=False),
            sa.Column("dedupe_key", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("updated_at", sa.String(), nullable=False),
            sa.Column("next_attempt_at", sa.String(), nullable=False),
            sa.Column("lease_owner", sa.String(), nullable=False, server_default=""),
            sa.Column("lease_expires_at", sa.String(), nullable=False, server_default=""),
            sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="6"),
            sa.Column("provider_operation_key", sa.String(), nullable=False, server_default=""),
            sa.Column("provider_id", sa.String(), nullable=False, server_default=""),
            sa.Column("external_id", sa.String(), nullable=False, server_default=""),
            sa.Column("external_url", sa.String(), nullable=False, server_default=""),
            sa.Column("action", sa.String(), nullable=False, server_default=""),
            sa.Column("error_code", sa.String(), nullable=False, server_default=""),
            _payload_column(),
            sa.PrimaryKeyConstraint("delivery_id"),
        )
    if not _index_exists(_TABLE, _DEDUPE_INDEX):
        op.create_index(_DEDUPE_INDEX, _TABLE, ["dedupe_key"], unique=True)
    if not _index_exists(_TABLE, _CLAIM_INDEX):
        op.create_index(
            _CLAIM_INDEX,
            _TABLE,
            ["state", "next_attempt_at", "lease_expires_at", "created_at"],
        )
    if not _index_exists(_TABLE, _AGGREGATE_INDEX):
        op.create_index(
            _AGGREGATE_INDEX,
            _TABLE,
            ["aggregate_type", "aggregate_id", sa.text("created_at DESC")],
        )
    if not _index_exists(_TABLE, _PROVIDER_KEY_INDEX):
        op.create_index(_PROVIDER_KEY_INDEX, _TABLE, ["provider_operation_key"])


def downgrade() -> None:
    if _table_exists(_TABLE):
        for index_name in (
            _PROVIDER_KEY_INDEX,
            _AGGREGATE_INDEX,
            _CLAIM_INDEX,
            _DEDUPE_INDEX,
        ):
            if _index_exists(_TABLE, index_name):
                op.drop_index(index_name, table_name=_TABLE)
        op.drop_table(_TABLE)
