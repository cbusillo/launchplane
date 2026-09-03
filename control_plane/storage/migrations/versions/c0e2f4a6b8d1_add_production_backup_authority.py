"""Add typed production backup targets and policies.

Revision ID: c0e2f4a6b8d1
Revises: b8d0f2a4c6e8
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c0e2f4a6b8d1"
down_revision: str | None = "b8d0f2a4c6e8"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_TARGETS_TABLE = "launchplane_production_backup_targets"
_POLICIES_TABLE = "launchplane_production_backup_policies"


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def _payload_column() -> sa.Column[object]:
    return sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        nullable=False,
    )


def upgrade() -> None:
    if not _table_exists(_TARGETS_TABLE):
        op.create_table(
            _TARGETS_TABLE,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("target_id", sa.String(), nullable=False),
            sa.Column("target_revision", sa.BigInteger(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("provider_type", sa.String(), nullable=False),
            sa.Column("destination_kind", sa.String(), nullable=False),
            sa.Column("effective_at", sa.String(), nullable=False),
            sa.Column("review_after", sa.String(), nullable=False),
            sa.Column("supersedes_record_id", sa.String(), nullable=True),
            sa.Column("target_digest", sa.String(), nullable=False),
            _payload_column(),
            sa.CheckConstraint(
                "status IN ('active', 'superseded', 'retired')",
                name="launchplane_production_backup_target_status_ck",
            ),
            sa.CheckConstraint(
                "target_revision >= 1",
                name="launchplane_production_backup_target_revision_ck",
            ),
            sa.CheckConstraint(
                "(target_revision = 1 AND supersedes_record_id IS NULL) OR "
                "(target_revision > 1 AND supersedes_record_id IS NOT NULL)",
                name="launchplane_production_backup_target_supersedes_ck",
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
        op.create_index(
            "launchplane_production_backup_target_revision_uidx",
            _TARGETS_TABLE,
            ["target_id", "target_revision"],
            unique=True,
        )
        op.create_index(
            "launchplane_production_backup_target_active_uidx",
            _TARGETS_TABLE,
            ["target_id"],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )
        op.create_index(
            "launchplane_production_backup_target_current_idx",
            _TARGETS_TABLE,
            ["target_id", "status", sa.text("target_revision DESC")],
            unique=False,
        )

    if not _table_exists(_POLICIES_TABLE):
        op.create_table(
            _POLICIES_TABLE,
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("policy_id", sa.String(), nullable=False),
            sa.Column("product", sa.String(), nullable=False),
            sa.Column("context", sa.String(), nullable=False),
            sa.Column("instance", sa.String(), nullable=False),
            sa.Column("promotion_action", sa.String(), nullable=False),
            sa.Column("policy_revision", sa.BigInteger(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("source_target_id", sa.String(), nullable=False),
            sa.Column("destination_target_id", sa.String(), nullable=False),
            sa.Column("effective_at", sa.String(), nullable=False),
            sa.Column("review_after", sa.String(), nullable=False),
            sa.Column("supersedes_record_id", sa.String(), nullable=True),
            sa.Column("policy_digest", sa.String(), nullable=False),
            _payload_column(),
            sa.CheckConstraint(
                "status IN ('active', 'superseded', 'retired')",
                name="launchplane_production_backup_policy_status_ck",
            ),
            sa.CheckConstraint(
                "policy_revision >= 1",
                name="launchplane_production_backup_policy_revision_ck",
            ),
            sa.CheckConstraint(
                "(policy_revision = 1 AND supersedes_record_id IS NULL) OR "
                "(policy_revision > 1 AND supersedes_record_id IS NOT NULL)",
                name="launchplane_production_backup_policy_supersedes_ck",
            ),
            sa.PrimaryKeyConstraint("record_id"),
        )
        op.create_index(
            "launchplane_production_backup_policy_revision_uidx",
            _POLICIES_TABLE,
            ["policy_id", "policy_revision"],
            unique=True,
        )
        op.create_index(
            "launchplane_production_backup_policy_active_uidx",
            _POLICIES_TABLE,
            ["product", "context", "instance", "promotion_action"],
            unique=True,
            sqlite_where=sa.text("status = 'active'"),
            postgresql_where=sa.text("status = 'active'"),
        )
        op.create_index(
            "launchplane_production_backup_policy_current_idx",
            _POLICIES_TABLE,
            [
                "product",
                "context",
                "instance",
                "promotion_action",
                "status",
                sa.text("policy_revision DESC"),
            ],
            unique=False,
        )


def downgrade() -> None:
    for table_name in (_POLICIES_TABLE, _TARGETS_TABLE):
        if not _table_exists(table_name):
            continue
        has_rows = bool(
            op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {table_name})"))
        )
        if has_rows:
            raise RuntimeError("Cannot downgrade production backup authority while records exist.")
    if _table_exists(_POLICIES_TABLE):
        op.drop_table(_POLICIES_TABLE)
    if _table_exists(_TARGETS_TABLE):
        op.drop_table(_TARGETS_TABLE)
