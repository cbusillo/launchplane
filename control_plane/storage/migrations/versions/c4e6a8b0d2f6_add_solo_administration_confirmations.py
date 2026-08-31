"""Add inert solo-administration confirmation records.

Revision ID: c4e6a8b0d2f6
Revises: c4e6a8b0d2f5
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c4e6a8b0d2f6"
down_revision: str | None = "c4e6a8b0d2f5"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_TABLE = "launchplane_solo_administration_confirmations"
_STATE_EXPIRY_INDEX = "launchplane_solo_administration_confirmation_state_expiry_idx"
_SESSION_INDEX = "launchplane_solo_administration_confirmation_session_idx"
_ISSUED_BINDING_INDEX = "launchplane_solo_administration_confirmation_issued_binding_uq"


def _table_exists() -> bool:
    return _TABLE in set(sa.inspect(op.get_bind()).get_table_names())


def _index_exists(index_name: str) -> bool:
    return _table_exists() and index_name in {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(_TABLE)
        if index.get("name") is not None
    }


def _payload_column() -> sa.Column[object]:
    return sa.Column(
        "payload",
        sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
        nullable=False,
    )


def _table_has_rows() -> bool:
    if not _table_exists():
        return False
    return bool(op.get_bind().scalar(sa.text(f"SELECT EXISTS (SELECT 1 FROM {_TABLE})")))


def upgrade() -> None:
    if not _table_exists():
        op.create_table(
            _TABLE,
            sa.Column("confirmation_id", sa.String(), nullable=False),
            sa.Column("state", sa.String(), nullable=False),
            sa.Column("active_policy_record_id", sa.String(), nullable=False),
            sa.Column("active_policy_revision", sa.BigInteger(), nullable=False),
            sa.Column("active_policy_sha256", sa.String(), nullable=False),
            sa.Column("candidate_policy_sha256", sa.String(), nullable=False),
            sa.Column("candidate_administrator_quorum", sa.BigInteger(), nullable=False),
            sa.Column(
                "candidate_distinct_human_administrator_count",
                sa.BigInteger(),
                nullable=False,
            ),
            sa.Column("reviewed_plan_sha256", sa.String(), nullable=False),
            sa.Column("human_session_id", sa.String(), nullable=False),
            sa.Column("github_id", sa.BigInteger(), nullable=False),
            sa.Column("idempotency_scope_sha256", sa.String(), nullable=False),
            sa.Column("idempotency_key_sha256", sa.String(), nullable=False),
            sa.Column("acknowledgement_sha256", sa.String(), nullable=False),
            sa.Column("created_at", sa.String(), nullable=False),
            sa.Column("expires_at", sa.String(), nullable=False),
            sa.Column("terminal_at", sa.String(), nullable=True),
            sa.Column("authority_state", sa.String(), nullable=False, server_default="inert"),
            sa.Column(
                "authorizes_policy",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
            _payload_column(),
            sa.CheckConstraint(
                "state IN ('issued', 'consumed', 'revoked', 'expired')",
                name="launchplane_solo_admin_confirmation_state_ck",
            ),
            sa.CheckConstraint(
                "active_policy_revision > 0 AND github_id > 0",
                name="launchplane_solo_admin_confirmation_positive_ids_ck",
            ),
            sa.CheckConstraint(
                "candidate_administrator_quorum = 1 AND "
                "candidate_distinct_human_administrator_count = 1",
                name="launchplane_solo_admin_confirmation_solo_quorum_ck",
            ),
            sa.CheckConstraint(
                "authority_state = 'inert' AND authorizes_policy = false",
                name="launchplane_solo_admin_confirmation_no_authority_ck",
            ),
            sa.CheckConstraint(
                "expires_at > created_at",
                name="launchplane_solo_admin_confirmation_expiry_ck",
            ),
            sa.CheckConstraint(
                "strftime('%s', expires_at) IS NOT NULL "
                "AND strftime('%s', created_at) IS NOT NULL "
                "AND CAST(strftime('%s', expires_at) AS INTEGER) "
                "- CAST(strftime('%s', created_at) AS INTEGER) = 300",
                name="lp_solo_admin_confirmation_ttl_sqlite_ck",
            ).ddl_if(dialect="sqlite"),
            sa.CheckConstraint(
                "CAST(expires_at AS timestamptz) - CAST(created_at AS timestamptz) "
                "= INTERVAL '5 minutes'",
                name="lp_solo_admin_confirmation_ttl_pg_ck",
            ).ddl_if(dialect="postgresql"),
            sa.CheckConstraint(
                "(state = 'issued' AND terminal_at IS NULL) OR "
                "(state IN ('consumed', 'revoked') AND terminal_at >= created_at "
                "AND terminal_at < expires_at) OR "
                "(state = 'expired' AND terminal_at >= expires_at)",
                name="launchplane_solo_admin_confirmation_terminal_ck",
            ),
            sa.PrimaryKeyConstraint("confirmation_id"),
        )
    if not _index_exists(_STATE_EXPIRY_INDEX):
        op.create_index(_STATE_EXPIRY_INDEX, _TABLE, ["state", "expires_at"])
    if not _index_exists(_SESSION_INDEX):
        op.create_index(_SESSION_INDEX, _TABLE, ["human_session_id", "created_at"])
    if not _index_exists(_ISSUED_BINDING_INDEX):
        op.create_index(
            _ISSUED_BINDING_INDEX,
            _TABLE,
            [
                "reviewed_plan_sha256",
                "human_session_id",
                "idempotency_scope_sha256",
                "idempotency_key_sha256",
            ],
            unique=True,
            postgresql_where=sa.text("state = 'issued'"),
            sqlite_where=sa.text("state = 'issued'"),
        )


def downgrade() -> None:
    if not _table_exists():
        return
    if _table_has_rows():
        raise RuntimeError(
            "Cannot downgrade solo-administration confirmation storage while records exist."
        )
    if _index_exists(_ISSUED_BINDING_INDEX):
        op.drop_index(_ISSUED_BINDING_INDEX, table_name=_TABLE)
    if _index_exists(_SESSION_INDEX):
        op.drop_index(_SESSION_INDEX, table_name=_TABLE)
    if _index_exists(_STATE_EXPIRY_INDEX):
        op.drop_index(_STATE_EXPIRY_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
