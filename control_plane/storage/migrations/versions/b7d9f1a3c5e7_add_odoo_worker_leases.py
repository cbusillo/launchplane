"""add odoo worker lease columns

Revision ID: b7d9f1a3c5e7
Revises: b6d8f0a2c4e6
Create Date: 2026-06-16 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "b7d9f1a3c5e7"
down_revision: str | None = "b6d8f0a2c4e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BOOTSTRAP_TABLE = "launchplane_odoo_stable_bootstrap_operations"
_REPLACEMENT_TABLE = "launchplane_odoo_stable_target_replacement_operations"
_BOOTSTRAP_CLAIM_INDEX = "launchplane_odoo_bootstrap_worker_claim_idx"
_REPLACEMENT_CLAIM_INDEX = "launchplane_odoo_replacement_worker_claim_idx"
_LEASE_COLUMNS = (
    ("lease_owner", sa.String(), ""),
    ("lease_expires_at", sa.String(), ""),
    ("heartbeat_at", sa.String(), ""),
    ("attempt", sa.Integer(), "0"),
)


def _table_exists(table_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return table_name in set(inspector.get_table_names())


def _column_exists(table_name: str, column_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return column_name in {str(column["name"]) for column in inspector.get_columns(table_name)}


def _index_exists(table_name: str, index_name: str) -> bool:
    connection = op.get_bind()
    inspector = sa.inspect(connection)
    return index_name in {str(index["name"]) for index in inspector.get_indexes(table_name)}


def _add_lease_columns(table_name: str) -> None:
    if not _table_exists(table_name):
        return
    for column_name, column_type, server_default in _LEASE_COLUMNS:
        if not _column_exists(table_name, column_name):
            op.add_column(
                table_name,
                sa.Column(
                    column_name,
                    column_type,
                    nullable=False,
                    server_default=server_default,
                ),
            )


def _drop_lease_columns(table_name: str) -> None:
    if not _table_exists(table_name):
        return
    for column_name, _, _ in reversed(_LEASE_COLUMNS):
        if _column_exists(table_name, column_name):
            op.drop_column(table_name, column_name)


def upgrade() -> None:
    _add_lease_columns(_BOOTSTRAP_TABLE)
    _add_lease_columns(_REPLACEMENT_TABLE)
    if _table_exists(_BOOTSTRAP_TABLE) and not _index_exists(
        _BOOTSTRAP_TABLE, _BOOTSTRAP_CLAIM_INDEX
    ):
        op.create_index(
            _BOOTSTRAP_CLAIM_INDEX,
            _BOOTSTRAP_TABLE,
            ["status", "lease_expires_at", "updated_at"],
        )
    if _table_exists(_REPLACEMENT_TABLE) and not _index_exists(
        _REPLACEMENT_TABLE, _REPLACEMENT_CLAIM_INDEX
    ):
        op.create_index(
            _REPLACEMENT_CLAIM_INDEX,
            _REPLACEMENT_TABLE,
            ["status", "lease_expires_at", "updated_at"],
        )


def downgrade() -> None:
    if _table_exists(_REPLACEMENT_TABLE) and _index_exists(
        _REPLACEMENT_TABLE, _REPLACEMENT_CLAIM_INDEX
    ):
        op.drop_index(_REPLACEMENT_CLAIM_INDEX, table_name=_REPLACEMENT_TABLE)
    if _table_exists(_BOOTSTRAP_TABLE) and _index_exists(
        _BOOTSTRAP_TABLE, _BOOTSTRAP_CLAIM_INDEX
    ):
        op.drop_index(_BOOTSTRAP_CLAIM_INDEX, table_name=_BOOTSTRAP_TABLE)
    _drop_lease_columns(_REPLACEMENT_TABLE)
    _drop_lease_columns(_BOOTSTRAP_TABLE)
