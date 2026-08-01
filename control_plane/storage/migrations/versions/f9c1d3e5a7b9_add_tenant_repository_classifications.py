"""add tenant repository classifications

Revision ID: f9c1d3e5a7b9
Revises: e8a0c2d4f6b8
Create Date: 2026-07-31 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "f9c1d3e5a7b9"
down_revision: str | None = "e8a0c2d4f6b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "launchplane_tenant_repository_classifications"
_REVISION_INDEX = "launchplane_tenant_repo_class_revision_uidx"
_CURRENT_INDEX = "launchplane_tenant_repo_class_current_idx"


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
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("repository_id", sa.String(), nullable=False),
            sa.Column("repository_owner_id", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("classification_kind", sa.String(), nullable=False),
            sa.Column("classification_revision", sa.BigInteger(), nullable=False),
            sa.Column("classified_at", sa.String(), nullable=False),
            sa.Column("classification_digest", sa.String(), nullable=False),
            sa.Column(
                "payload",
                sa.JSON().with_variant(
                    postgresql.JSONB(astext_type=sa.Text()),
                    "postgresql",
                ),
                nullable=False,
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
    if not _index_exists(_REVISION_INDEX):
        op.create_index(
            _REVISION_INDEX,
            _TABLE,
            ["repository_id", "classification_revision"],
            unique=True,
        )
    if not _index_exists(_CURRENT_INDEX):
        op.create_index(
            _CURRENT_INDEX,
            _TABLE,
            ["repository_id", sa.text("classification_revision DESC")],
        )


def downgrade() -> None:
    if not _table_exists():
        return
    for index_name in (_CURRENT_INDEX, _REVISION_INDEX):
        if _index_exists(index_name):
            op.drop_index(index_name, table_name=_TABLE)
    op.drop_table(_TABLE)
