"""add repository inventory records

Revision ID: f2241a0b1c2d
Revises: f2239a0b1c2d
Create Date: 2026-08-26 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f2241a0b1c2d"
down_revision: str | None = "f2239a0b1c2d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_repository_inventory_records"
_REVISION_INDEX = "launchplane_repository_inventory_revision_uidx"
_CURRENT_INDEX = "launchplane_repository_inventory_current_idx"


def _table_exists() -> bool:
    return _TABLE in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists(index_name: str) -> bool:
    return _table_exists() and index_name in {
        str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
    }


def upgrade() -> None:
    if not _table_exists():
        op.create_table(
            _TABLE,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("repository_id", sa.String(), nullable=False),
            sa.Column("repository_owner_id", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("inventory_state", sa.String(), nullable=False),
            sa.Column("inventory_revision", sa.BigInteger(), nullable=False),
            sa.Column("recorded_at", sa.String(), nullable=False),
            sa.Column("inventory_digest", sa.String(), nullable=False),
            sa.Column(
                "payload",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
            ),
            sa.CheckConstraint(
                "inventory_state IN ('tracked', 'retired')",
                name="launchplane_repository_inventory_state_ck",
            ),
            sa.CheckConstraint(
                "inventory_revision >= 1",
                name="launchplane_repository_inventory_revision_ck",
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
    if not _index_exists(_REVISION_INDEX):
        op.create_index(
            _REVISION_INDEX, _TABLE, ["repository_id", "inventory_revision"], unique=True
        )
    if not _index_exists(_CURRENT_INDEX):
        op.create_index(_CURRENT_INDEX, _TABLE, ["repository_id", sa.text("inventory_revision DESC")])


def downgrade() -> None:
    if not _table_exists():
        return
    for index_name in (_CURRENT_INDEX, _REVISION_INDEX):
        if _index_exists(index_name):
            op.drop_index(index_name, table_name=_TABLE)
    op.drop_table(_TABLE)
