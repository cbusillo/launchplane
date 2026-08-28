"""Add the owner-control active-challenge operation guard.

Revision ID: f6a1c3e5b7d9
Revises: e5f7a9b1c3d6
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "f6a1c3e5b7d9"
down_revision: str | None = "e5f7a9b1c3d6"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_TABLE = "launchplane_owner_control_issued_challenges"
_INDEX = "launchplane_owner_control_challenge_active_operation_uidx"


def _index_exists() -> bool:
    return _INDEX in {
        str(name)
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
        if (name := index.get("name")) is not None
    }


def upgrade() -> None:
    if not _index_exists():
        op.create_index(
            _INDEX,
            _TABLE,
            ["operation_id"],
            unique=True,
            postgresql_where=sa.text("state = 'issued'"),
            sqlite_where=sa.text("state = 'issued'"),
        )


def downgrade() -> None:
    if _index_exists():
        op.drop_index(_INDEX, table_name=_TABLE)
