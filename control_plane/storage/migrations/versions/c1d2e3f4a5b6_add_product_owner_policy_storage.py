"""add product owner policy storage

Revision ID: c1d2e3f4a5b6
Revises: b1c2d3e4f5a6
Create Date: 2026-08-05 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import TypeEngine

revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b1c2d3e4f5a6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_POLICY_TABLE = "launchplane_product_owner_policies"
_REQUIREMENT_TABLE = "launchplane_product_owner_requirements"
_ROUTING_TABLE = "launchplane_product_owner_routing"

_POLICY_REVISION_INDEX = "launchplane_product_owner_policy_revision_uidx"
_POLICY_ACTIVE_INDEX = "launchplane_product_owner_policy_active_uidx"
_POLICY_CURRENT_INDEX = "launchplane_product_owner_policy_current_idx"
_REQUIREMENT_REVISION_INDEX = "launchplane_product_owner_requirement_revision_uidx"
_REQUIREMENT_ACTIVE_INDEX = "launchplane_product_owner_requirement_active_uidx"
_REQUIREMENT_CURRENT_INDEX = "launchplane_product_owner_requirement_current_idx"
_ROUTING_REVISION_INDEX = "launchplane_product_owner_routing_revision_uidx"
_ROUTING_ACTIVE_INDEX = "launchplane_product_owner_routing_active_uidx"
_ROUTING_CURRENT_INDEX = "launchplane_product_owner_routing_current_idx"


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
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("system", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("policy_revision", sa.BigInteger(), nullable=False),
            sa.Column("quorum", sa.BigInteger(), nullable=False),
            sa.Column("effective_at", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("supersedes_record_id", sa.String(), nullable=True),
            sa.Column("policy_digest", sa.String(), nullable=False),
            sa.Column("payload", _json_payload_type(), nullable=False),
            sa.CheckConstraint(
                "status IN ('active', 'superseded')",
                name="launchplane_product_owner_policy_status_ck",
            ),
            sa.CheckConstraint(
                "policy_revision >= 1",
                name="launchplane_product_owner_policy_revision_ck",
            ),
            sa.CheckConstraint(
                "quorum = 1",
                name="launchplane_product_owner_policy_quorum_ck",
            ),
            sa.CheckConstraint(
                "(policy_revision = 1 AND supersedes_record_id IS NULL) OR "
                "(policy_revision > 1 AND supersedes_record_id IS NOT NULL)",
                name="launchplane_product_owner_policy_supersedes_ck",
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
    _create_revision_indexes(
        table_name=_POLICY_TABLE,
        revision_column="policy_revision",
        revision_index=_POLICY_REVISION_INDEX,
        active_index=_POLICY_ACTIVE_INDEX,
        current_index=_POLICY_CURRENT_INDEX,
    )

    if not _table_exists(_REQUIREMENT_TABLE):
        op.create_table(
            _REQUIREMENT_TABLE,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("system", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("requirement_revision", sa.BigInteger(), nullable=False),
            sa.Column("enforcement_mode", sa.String(), nullable=False),
            sa.Column("effective_at", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("supersedes_record_id", sa.String(), nullable=True),
            sa.Column("requirement_digest", sa.String(), nullable=False),
            sa.Column("payload", _json_payload_type(), nullable=False),
            sa.CheckConstraint(
                "status IN ('active', 'superseded')",
                name="launchplane_product_owner_requirement_status_ck",
            ),
            sa.CheckConstraint(
                "requirement_revision >= 1",
                name="launchplane_product_owner_requirement_revision_ck",
            ),
            sa.CheckConstraint(
                "enforcement_mode = 'shadow'",
                name="launchplane_product_owner_requirement_shadow_ck",
            ),
            sa.CheckConstraint(
                "(requirement_revision = 1 AND supersedes_record_id IS NULL) OR "
                "(requirement_revision > 1 AND supersedes_record_id IS NOT NULL)",
                name="launchplane_product_owner_requirement_supersedes_ck",
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
    _create_revision_indexes(
        table_name=_REQUIREMENT_TABLE,
        revision_column="requirement_revision",
        revision_index=_REQUIREMENT_REVISION_INDEX,
        active_index=_REQUIREMENT_ACTIVE_INDEX,
        current_index=_REQUIREMENT_CURRENT_INDEX,
    )

    if not _table_exists(_ROUTING_TABLE):
        op.create_table(
            _ROUTING_TABLE,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("system", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("routing_revision", sa.BigInteger(), nullable=False),
            sa.Column("authoritative", sa.Boolean(), nullable=False),
            sa.Column("effective_at", sa.String(), nullable=False),
            sa.Column("source", sa.String(), nullable=False),
            sa.Column("supersedes_record_id", sa.String(), nullable=True),
            sa.Column("routing_digest", sa.String(), nullable=False),
            sa.Column("payload", _json_payload_type(), nullable=False),
            sa.CheckConstraint(
                "status IN ('active', 'superseded')",
                name="launchplane_product_owner_routing_status_ck",
            ),
            sa.CheckConstraint(
                "routing_revision >= 1",
                name="launchplane_product_owner_routing_revision_ck",
            ),
            sa.CheckConstraint(
                "authoritative = false",
                name="launchplane_product_owner_routing_non_authoritative_ck",
            ),
            sa.CheckConstraint(
                "(routing_revision = 1 AND supersedes_record_id IS NULL) OR "
                "(routing_revision > 1 AND supersedes_record_id IS NOT NULL)",
                name="launchplane_product_owner_routing_supersedes_ck",
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
    _create_revision_indexes(
        table_name=_ROUTING_TABLE,
        revision_column="routing_revision",
        revision_index=_ROUTING_REVISION_INDEX,
        active_index=_ROUTING_ACTIVE_INDEX,
        current_index=_ROUTING_CURRENT_INDEX,
    )


def _create_revision_indexes(
    *,
    table_name: str,
    revision_column: str,
    revision_index: str,
    active_index: str,
    current_index: str,
) -> None:
    if not _index_exists(table_name, revision_index):
        op.create_index(
            revision_index,
            table_name,
            ["product", "system", revision_column],
            unique=True,
        )
    if not _index_exists(table_name, active_index):
        op.create_index(
            active_index,
            table_name,
            ["product", "system"],
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
            sqlite_where=sa.text("status = 'active'"),
        )
    if not _index_exists(table_name, current_index):
        op.create_index(
            current_index,
            table_name,
            ["product", "system", "status", sa.text(f"{revision_column} DESC")],
        )


def downgrade() -> None:
    for table_name, indexes in (
        (
            _ROUTING_TABLE,
            (_ROUTING_CURRENT_INDEX, _ROUTING_ACTIVE_INDEX, _ROUTING_REVISION_INDEX),
        ),
        (
            _REQUIREMENT_TABLE,
            (
                _REQUIREMENT_CURRENT_INDEX,
                _REQUIREMENT_ACTIVE_INDEX,
                _REQUIREMENT_REVISION_INDEX,
            ),
        ),
        (
            _POLICY_TABLE,
            (_POLICY_CURRENT_INDEX, _POLICY_ACTIVE_INDEX, _POLICY_REVISION_INDEX),
        ),
    ):
        if not _table_exists(table_name):
            continue
        for index_name in indexes:
            if _index_exists(table_name, index_name):
                op.drop_index(index_name, table_name=table_name)
        op.drop_table(table_name)
