"""add hardware-backed authorization recovery records

Revision ID: f2239a0b1c2d
Revises: e2221b0c2d3e
Create Date: 2026-08-25 00:00:00.000000+00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "f2239a0b1c2d"
down_revision: str | None = "e2221b0c2d3e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists(table_name: str, index_name: str) -> bool:
    return index_name in {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table_name)}


def _payload_column() -> sa.Column[object]:
    return sa.Column("payload", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=False)


def upgrade() -> None:
    if not _table_exists("launchplane_authorization_recovery_keys"):
        op.create_table(
            "launchplane_authorization_recovery_keys",
            sa.Column("key_id", sa.String(), nullable=False),
            sa.Column("custody_slot", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("fingerprint_sha256", sa.String(), nullable=False),
            sa.Column("enrolled_at", sa.String(), nullable=False),
            _payload_column(),
            sa.PrimaryKeyConstraint("key_id"),
        )
    if not _index_exists("launchplane_authorization_recovery_keys", "launchplane_authorization_recovery_keys_slot_idx"):
        op.create_index("launchplane_authorization_recovery_keys_slot_idx", "launchplane_authorization_recovery_keys", ["custody_slot"])
    if not _index_exists("launchplane_authorization_recovery_keys", "launchplane_authorization_recovery_keys_status_idx"):
        op.create_index("launchplane_authorization_recovery_keys_status_idx", "launchplane_authorization_recovery_keys", ["status", "enrolled_at"])
    if not _table_exists("launchplane_authorization_bootstrap"):
        op.create_table(
            "launchplane_authorization_bootstrap",
            sa.Column("record_id", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("completed_at", sa.String(), nullable=False, server_default=""),
            _payload_column(),
            sa.PrimaryKeyConstraint("record_id"),
        )
    if not _table_exists("launchplane_authorization_recovery_challenges"):
        op.create_table(
            "launchplane_authorization_recovery_challenges",
            sa.Column("challenge_id", sa.String(), nullable=False),
            sa.Column("operation", sa.String(), nullable=False),
            sa.Column("expires_at", sa.String(), nullable=False),
            sa.Column("used_at", sa.String(), nullable=False, server_default=""),
            _payload_column(),
            sa.PrimaryKeyConstraint("challenge_id"),
        )
    if not _index_exists("launchplane_authorization_recovery_challenges", "launchplane_authorization_recovery_challenges_expiry_idx"):
        op.create_index("launchplane_authorization_recovery_challenges_expiry_idx", "launchplane_authorization_recovery_challenges", ["expires_at", "used_at"])
    if not _table_exists("launchplane_authorization_recovery_audits"):
        op.create_table(
            "launchplane_authorization_recovery_audits",
            sa.Column("audit_id", sa.String(), nullable=False),
            sa.Column("event", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("recorded_at", sa.String(), nullable=False),
            _payload_column(),
            sa.PrimaryKeyConstraint("audit_id"),
        )
    if not _index_exists("launchplane_authorization_recovery_audits", "launchplane_authorization_recovery_audits_recorded_idx"):
        op.create_index("launchplane_authorization_recovery_audits_recorded_idx", "launchplane_authorization_recovery_audits", [sa.text("recorded_at DESC")])


def downgrade() -> None:
    for table_name, indexes in (
        ("launchplane_authorization_recovery_audits", ("launchplane_authorization_recovery_audits_recorded_idx",)),
        ("launchplane_authorization_recovery_challenges", ("launchplane_authorization_recovery_challenges_expiry_idx",)),
        ("launchplane_authorization_recovery_keys", ("launchplane_authorization_recovery_keys_status_idx", "launchplane_authorization_recovery_keys_slot_idx")),
    ):
        if _table_exists(table_name):
            for index_name in indexes:
                if _index_exists(table_name, index_name):
                    op.drop_index(index_name, table_name=table_name)
            op.drop_table(table_name)
    if _table_exists("launchplane_authorization_bootstrap"):
        op.drop_table("launchplane_authorization_bootstrap")
