"""add change impact policy storage

Revision ID: d2e4f6a8b0c2
Revises: c1d2e3f4a5b6
Create Date: 2026-08-06 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import TypeEngine


revision: str = "d2e4f6a8b0c2"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY_TABLE = "launchplane_change_impact_policies"
_REVISION_INDEX = "launchplane_change_impact_policy_revision_uidx"
_ACTIVE_INDEX = "launchplane_change_impact_policy_active_uidx"
_CURRENT_INDEX = "launchplane_change_impact_policy_current_idx"


def _json_payload_type() -> TypeEngine[object]:
    return sa.JSON().with_variant(
        postgresql.JSONB(astext_type=sa.Text()),
        "postgresql",
    )


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    if not _table_exists(table_name):
        return False
    return index_name in {
        str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table_name)
    }


def upgrade() -> None:
    if not _table_exists(_POLICY_TABLE):
        op.create_table(
            _POLICY_TABLE,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("repository_id", sa.String(), nullable=False),
            sa.Column("repository_owner_id", sa.String(), nullable=False),
            sa.Column("repository", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("policy_revision", sa.BigInteger(), nullable=False),
            sa.Column("default_unknown_review_tier", sa.String(), nullable=False),
            sa.Column("effective_at", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("supersedes_record_id", sa.String(), nullable=True),
            sa.Column("policy_digest", sa.String(), nullable=False),
            sa.Column("payload", _json_payload_type(), nullable=False),
            sa.CheckConstraint(
                "status IN ('active', 'superseded')",
                name="launchplane_change_impact_policy_status_ck",
            ),
            sa.CheckConstraint(
                "policy_revision >= 1",
                name="launchplane_change_impact_policy_revision_ck",
            ),
            sa.CheckConstraint(
                "default_unknown_review_tier = 'sensitive'",
                name="launchplane_change_impact_policy_unknown_tier_ck",
            ),
            sa.CheckConstraint(
                "(policy_revision = 1 AND supersedes_record_id IS NULL) OR "
                "(policy_revision > 1 AND supersedes_record_id IS NOT NULL)",
                name="launchplane_change_impact_policy_supersedes_ck",
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
    if not _index_exists(_POLICY_TABLE, _REVISION_INDEX):
        op.create_index(
            _REVISION_INDEX,
            _POLICY_TABLE,
            ["repository_id", "policy_revision"],
            unique=True,
        )
    if not _index_exists(_POLICY_TABLE, _ACTIVE_INDEX):
        op.create_index(
            _ACTIVE_INDEX,
            _POLICY_TABLE,
            ["repository_id"],
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        )
    if not _index_exists(_POLICY_TABLE, _CURRENT_INDEX):
        op.create_index(
            _CURRENT_INDEX,
            _POLICY_TABLE,
            ["repository_id", "status", sa.text("policy_revision DESC")],
        )


def downgrade() -> None:
    if _index_exists(_POLICY_TABLE, _CURRENT_INDEX):
        op.drop_index(_CURRENT_INDEX, table_name=_POLICY_TABLE)
    if _index_exists(_POLICY_TABLE, _ACTIVE_INDEX):
        op.drop_index(_ACTIVE_INDEX, table_name=_POLICY_TABLE)
    if _index_exists(_POLICY_TABLE, _REVISION_INDEX):
        op.drop_index(_REVISION_INDEX, table_name=_POLICY_TABLE)
    if _table_exists(_POLICY_TABLE):
        op.drop_table(_POLICY_TABLE)
