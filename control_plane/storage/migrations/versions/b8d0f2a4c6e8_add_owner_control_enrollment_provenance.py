"""Add immutable owner-control enrollment provenance.

Revision ID: b8d0f2a4c6e8
Revises: fbc9d1e3a5b7
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b8d0f2a4c6e8"
down_revision: str | None = "fbc9d1e3a5b7"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_SESSIONS_TABLE = "launchplane_owner_control_channel_sessions"
_PROVENANCE_TABLE = "launchplane_owner_control_enrollment_provenance"


def _table_exists(table_name: str) -> bool:
    return table_name in set(sa.inspect(op.get_bind()).get_table_names())


def _payload_column() -> sa.Column[object]:
    return sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        nullable=False,
    )


def _session_rows_lack_provenance() -> bool:
    if not _table_exists(_SESSIONS_TABLE):
        return False
    if not _table_exists(_PROVENANCE_TABLE):
        return bool(
            op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {_SESSIONS_TABLE})"))
        )
    return bool(
        op.get_bind().scalar(
            sa.text(
                f"SELECT EXISTS ("
                f"SELECT 1 FROM {_SESSIONS_TABLE} AS sessions "
                f"LEFT JOIN {_PROVENANCE_TABLE} AS provenance "
                "ON provenance.channel_session_id = sessions.channel_session_id "
                "WHERE provenance.channel_session_id IS NULL"
                ")"
            )
        )
    )


def _provenance_rows_exist() -> bool:
    if not _table_exists(_PROVENANCE_TABLE):
        return False
    return bool(op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {_PROVENANCE_TABLE})")))


def upgrade() -> None:
    if _session_rows_lack_provenance():
        raise RuntimeError(
            "Cannot add owner-control enrollment provenance while legacy session rows exist."
        )
    if _table_exists(_PROVENANCE_TABLE):
        return
    op.create_table(
        _PROVENANCE_TABLE,
        sa.Column("channel_session_id", sa.String(), nullable=False),
        sa.Column("owner_github_id", sa.BigInteger(), nullable=False),
        sa.Column("binding_sha256", sa.String(), nullable=False),
        sa.Column("host_principal_claim_sha256", sa.String(), nullable=False),
        sa.Column("enrolled_at", sa.String(), nullable=False),
        sa.Column(
            "enrollment_context",
            sa.String(),
            nullable=False,
            server_default="postgres_record_store",
        ),
        sa.Column(
            "server_observed_corroboration",
            sa.String(),
            nullable=False,
            server_default="none",
        ),
        sa.Column(
            "provenance_tier",
            sa.String(),
            nullable=False,
            server_default="self_asserted",
        ),
        sa.Column("authority_state", sa.String(), nullable=False, server_default="inert"),
        sa.Column(
            "authorizes_execution",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        _payload_column(),
        sa.CheckConstraint(
            "enrollment_context = 'postgres_record_store'",
            name="launchplane_owner_control_provenance_context_ck",
        ),
        sa.CheckConstraint(
            "server_observed_corroboration = 'none'",
            name="launchplane_owner_control_provenance_corroboration_ck",
        ),
        sa.CheckConstraint(
            "provenance_tier = 'self_asserted'",
            name="launchplane_owner_control_provenance_tier_ck",
        ),
        sa.CheckConstraint(
            "authority_state = 'inert'",
            name="launchplane_owner_control_provenance_authority_ck",
        ),
        sa.CheckConstraint(
            "authorizes_execution = false",
            name="launchplane_owner_control_provenance_authorization_ck",
        ),
        sa.ForeignKeyConstraint(
            ["channel_session_id"],
            [f"{_SESSIONS_TABLE}.channel_session_id"],
            name="launchplane_owner_control_provenance_session_fk",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("channel_session_id"),
        sa.UniqueConstraint(
            "owner_github_id",
            "binding_sha256",
            "host_principal_claim_sha256",
            name="launchplane_owner_control_provenance_binding_claim_uq",
        ),
    )


def downgrade() -> None:
    if _provenance_rows_exist():
        raise RuntimeError(
            "Cannot downgrade owner-control enrollment provenance while records exist."
        )
    if _table_exists(_PROVENANCE_TABLE):
        op.drop_table(_PROVENANCE_TABLE)
