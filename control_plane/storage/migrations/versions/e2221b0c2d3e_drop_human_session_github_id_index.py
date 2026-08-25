"""drop obsolete human-session GitHub ID lookup index

Revision ID: e2221b0c2d3e
Revises: d2219a0b1c2d
Create Date: 2026-08-25 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "e2221b0c2d3e"
down_revision: str | None = "d2219a0b1c2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_human_sessions"
_INDEX = "launchplane_human_sessions_github_id_idx"


def _index_names() -> set[str] | None:
    inspector = sa.inspect(op.get_bind())
    if _TABLE not in set(inspector.get_table_names()):
        return None
    return {str(index["name"]) for index in inspector.get_indexes(_TABLE)}


def upgrade() -> None:
    index_names = _index_names()
    if index_names is not None and _INDEX in index_names:
        op.drop_index(_INDEX, table_name=_TABLE)


def downgrade() -> None:
    index_names = _index_names()
    if index_names is not None and _INDEX not in index_names:
        op.create_index(_INDEX, _TABLE, ["github_id"])
