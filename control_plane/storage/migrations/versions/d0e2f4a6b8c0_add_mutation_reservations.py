"""add mutation reservations

Revision ID: d0e2f4a6b8c0
Revises: c9d1e3f5a7b9
Create Date: 2026-07-13 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "d0e2f4a6b8c0"
down_revision: str | None = "c9d1e3f5a7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_idempotency_records"
_STATE_LEASE_INDEX = "launchplane_idempotency_state_lease_idx"


def _column_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {str(column["name"]) for column in inspector.get_columns(_TABLE)}


def _index_exists(index_name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return index_name in {str(index["name"]) for index in inspector.get_indexes(_TABLE)}


def upgrade() -> None:
    existing_columns = _column_names()
    with op.batch_alter_table(_TABLE) as batch_op:
        if "state" not in existing_columns:
            batch_op.add_column(
                sa.Column("state", sa.String(), server_default="completed", nullable=False)
            )
        if "lease_owner" not in existing_columns:
            batch_op.add_column(
                sa.Column("lease_owner", sa.String(), server_default="", nullable=False)
            )
        if "lease_expires_at" not in existing_columns:
            batch_op.add_column(
                sa.Column("lease_expires_at", sa.String(), server_default="", nullable=False)
            )
        if "attempt" not in existing_columns:
            batch_op.add_column(
                sa.Column("attempt", sa.Integer(), server_default="1", nullable=False)
            )
        if "reconciliation_key" not in existing_columns:
            batch_op.add_column(
                sa.Column("reconciliation_key", sa.String(), server_default="", nullable=False)
            )
        if "created_at" not in existing_columns:
            batch_op.add_column(
                sa.Column("created_at", sa.String(), server_default="", nullable=False)
            )
        if "updated_at" not in existing_columns:
            batch_op.add_column(
                sa.Column("updated_at", sa.String(), server_default="", nullable=False)
            )
        batch_op.alter_column(
            "response_status_code",
            existing_type=sa.Integer(),
            nullable=True,
        )
    op.execute(
        sa.text(
            "UPDATE launchplane_idempotency_records "
            "SET created_at = recorded_at, updated_at = recorded_at "
            "WHERE created_at = '' OR updated_at = ''"
        )
    )
    if not _index_exists(_STATE_LEASE_INDEX):
        op.create_index(
            _STATE_LEASE_INDEX,
            _TABLE,
            ["state", "lease_expires_at", "updated_at"],
        )


def downgrade() -> None:
    connection = op.get_bind()
    active_reservations = int(
        connection.execute(
            sa.text(
                "SELECT count(*) FROM launchplane_idempotency_records WHERE state <> 'completed'"
            )
        ).scalar_one()
    )
    incomplete_responses = int(
        connection.execute(
            sa.text(
                "SELECT count(*) FROM launchplane_idempotency_records "
                "WHERE response_status_code IS NULL"
            )
        ).scalar_one()
    )
    if active_reservations or incomplete_responses:
        raise RuntimeError(
            "Cannot downgrade mutation reservations while active or incomplete reservations exist."
        )
    if _index_exists(_STATE_LEASE_INDEX):
        op.drop_index(_STATE_LEASE_INDEX, table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.alter_column(
            "response_status_code",
            existing_type=sa.Integer(),
            nullable=False,
        )
        batch_op.drop_column("updated_at")
        batch_op.drop_column("created_at")
        batch_op.drop_column("reconciliation_key")
        batch_op.drop_column("attempt")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_owner")
        batch_op.drop_column("state")
